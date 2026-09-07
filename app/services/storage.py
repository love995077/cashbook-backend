"""Where uploaded photos and voice notes are kept.

One function the routers care about — `store_upload` — and one local backend
behind it. The backend is a seam, not a placeholder: the local one is fully
implemented and works today.

------------------------------------------------------------------------------
WHAT IS STORED, AND WHY IT IS A PATH RATHER THAN A URL
------------------------------------------------------------------------------
The database column holds a storage-relative path — "photos/7/2026/09/ab12.jpg"
— not "https://api.example.com/media/photos/...". A stored absolute URL bakes
the hostname into every row, so moving to a CDN, changing domain, or running
the same database behind a staging host silently breaks every old link. The
public URL is derived at read time by `public_url`, which is one place to
change instead of a migration over the whole table.

The API still exposes the field as `..._url` in its JSON, so clients get
something they can fetch directly.

------------------------------------------------------------------------------
OWNERSHIP IS ENCODED IN THE PATH
------------------------------------------------------------------------------
Every key starts "<kind>/<owner_id>/", and `is_owned_by` checks that prefix.
This matters for the two-step audio flow: the client uploads a note, gets a
path back, and later attaches that path to a transaction or an advance
request. Without the check, a caller could attach somebody else's recording by
guessing its key. Nothing else guards that path — do not remove it.

------------------------------------------------------------------------------
MOVING TO S3 / R2 / SPACES
------------------------------------------------------------------------------
Set MEDIA_BACKEND=s3 and implement `_S3Backend` below. The shape is already
right: `save` uploads the bytes and `public_url` returns the CDN link.

    import boto3

    client = boto3.client(
        "s3",
        region_name=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url or None,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
    )
    client.put_object(
        Bucket=settings.s3_bucket, Key=key, Body=payload, ContentType=content_type
    )

Things that will bite when you do:
  * Serving private objects needs presigned URLs, which expire — the app would
    have to refresh them, so a public-read bucket behind a CDN is simpler for
    site photos.
  * Deleting a row should delete the object, and the two are not in one
    transaction. A periodic sweep for orphans is the usual answer; this module
    deliberately does not pretend otherwise.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Extensions we are willing to write, mapped from the content type the phone
# sends. An allow-list rather than trusting the client's filename, which is
# attacker-controlled and the classic way to write a .php into a served
# directory.
IMAGE_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

AUDIO_TYPES: dict[str, str] = {
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "video/mp4": ".m4a",  # what iOS sometimes labels an m4a container
}

# Server-generated only, unlike the two above: nothing a phone uploads lands
# here. It exists so a rendered invoice can be filed next to the row that
# refers to it instead of being rebuilt on every share.
DOCUMENT_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
}

_TABLES: dict[str, dict[str, str]] = {
    "photos": IMAGE_TYPES,
    "audio": AUDIO_TYPES,
    "documents": DOCUMENT_TYPES,
}

_KIND_NOUNS = {"photos": "image", "audio": "audio", "documents": "document"}


class StorageError(RuntimeError):
    """The upload could not be stored."""


class UnsupportedMedia(ValueError):
    """The content type is not on the allow-list for this kind of upload."""


@dataclass(frozen=True)
class StoredFile:
    """What the caller records in the database."""

    #: Storage-relative key, e.g. "photos/7/2026/09/1f3c….jpg". Goes in the DB.
    path: str
    #: Fetchable link derived from `path`. Never stored.
    url: str
    content_type: str
    size_bytes: int


def _extension_for(kind: str, content_type: Optional[str]) -> str:
    table = _TABLES.get(kind)
    if table is None:
        raise UnsupportedMedia(f"Unknown upload kind: {kind}")

    base = (content_type or "").split(";")[0].strip().lower()
    if base not in table:
        allowed = ", ".join(sorted(set(table)))
        raise UnsupportedMedia(
            f"Unsupported {_KIND_NOUNS[kind]} type: "
            f"{content_type or 'unknown'}. Expected one of: {allowed}"
        )
    return table[base]


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str, limit: int = 40) -> str:
    """A filesystem-safe fragment of a caption, for a recognisable filename.

    Cosmetic only — the uuid is what makes the name unique. Non-ASCII (a Hindi
    caption, typically) reduces to nothing, and that is fine: the result is
    just dropped from the filename.
    """
    ascii_only = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    )
    return _SLUG_STRIP.sub("-", ascii_only.lower()).strip("-")[:limit]


def build_key(kind: str, owner_id: int, extension: str, label: str = "") -> str:
    """The storage key for one upload.

    Sharded by year and month so a long-running site does not end up with one
    directory holding tens of thousands of entries, which is slow to list on
    most filesystems.
    """
    now = datetime.now(timezone.utc)
    slug = slugify(label)
    name = f"{uuid.uuid4().hex}{'-' + slug if slug else ''}{extension}"
    return f"{kind}/{owner_id}/{now:%Y}/{now:%m}/{name}"


def is_owned_by(path: str, owner_id: int) -> bool:
    """Whether `path` was uploaded by `owner_id`.

    The only thing standing between a client-supplied media path and attaching
    another user's recording to your own row. See the module docstring.
    """
    if not path:
        return False
    parts = path.split("/")
    return len(parts) > 2 and parts[1] == str(owner_id)


class _LocalBackend:
    """Files on disk, served by the app's own StaticFiles mount."""

    def __init__(self, root: Path, url_prefix: str) -> None:
        self.root = root
        self.url_prefix = url_prefix.rstrip("/")

    def _absolute(self, key: str) -> Path:
        target = (self.root / key).resolve()
        # Defence in depth. Keys are built here, not supplied by callers, but a
        # future caller passing "../../etc/passwd" must not escape the root.
        if not target.is_relative_to(self.root):
            raise StorageError("Refusing to write outside the media directory")
        return target

    def save(self, key: str, payload: bytes) -> None:
        target = self._absolute(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename: a crash mid-write then leaves a
        # stray .part rather than a truncated JPEG the app would serve as real.
        staging = target.with_suffix(target.suffix + ".part")
        staging.write_bytes(payload)
        staging.replace(target)

    def delete(self, key: str) -> None:
        try:
            self._absolute(key).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete media file %s", key, exc_info=True)

    def public_url(self, key: str) -> str:
        return f"{self.url_prefix}/{key}"


class _S3Backend:
    """NOT IMPLEMENTED. See the module docstring for the shape it should take."""

    def save(self, key: str, payload: bytes) -> None:
        raise StorageError(
            "MEDIA_BACKEND=s3 is configured but the S3 backend is not implemented. "
            "Implement _S3Backend in app/services/storage.py, or set "
            "MEDIA_BACKEND=local."
        )

    def delete(self, key: str) -> None:
        raise StorageError("The S3 backend is not implemented")

    def public_url(self, key: str) -> str:
        base = settings.s3_public_base_url.rstrip("/")
        return f"{base}/{key}" if base else key


def _make_backend():
    if settings.media_backend == "s3":
        return _S3Backend()
    return _LocalBackend(settings.media_path, settings.media_url_prefix)


_backend = _make_backend()


def public_url(path: Optional[str]) -> Optional[str]:
    """The fetchable link for a stored path, or None for a row without one."""
    if not path:
        return None
    return _backend.public_url(path)


def store_upload(
    payload: bytes,
    *,
    kind: str,
    owner_id: int,
    content_type: Optional[str],
    label: str = "",
) -> StoredFile:
    """Write one upload and return what to record against it.

    Args:
        payload: The bytes, already read and size-checked by the caller.
        kind: "photos" or "audio" — picks the allow-list and the key prefix.
        owner_id: Who uploaded it. Encoded into the key; see `is_owned_by`.
        content_type: What the client claimed. Checked against the allow-list.
        label: Optional caption, used only to make the filename readable.

    Raises:
        UnsupportedMedia: The content type is not allowed for this kind.
        StorageError: The backend could not write it.
    """
    extension = _extension_for(kind, content_type)
    key = build_key(kind, owner_id, extension, label)

    try:
        _backend.save(key, payload)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(f"Could not store the upload: {exc}") from exc

    return StoredFile(
        path=key,
        url=_backend.public_url(key),
        content_type=(content_type or "").split(";")[0].strip().lower(),
        size_bytes=len(payload),
    )


def delete_stored(path: Optional[str]) -> None:
    """Best-effort removal. Never raises — the row is already gone."""
    if not path:
        return
    try:
        _backend.delete(path)
    except Exception:
        logger.warning("Could not delete stored media %s", path, exc_info=True)
