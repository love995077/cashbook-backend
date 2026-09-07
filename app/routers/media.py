"""Uploading short voice notes.

Two steps rather than one, deliberately. The client uploads the recording
here, gets a `path` back, and sends that path in the JSON body of whatever it
is attaching to — an advance request, a transaction, a dispute.

The alternative was multipart on each of those endpoints. This is better for
one practical reason: a voice note is recorded while the form is still being
filled in, so uploading it early means the slow part is already done when the
contractor taps Save. It also keeps three JSON APIs from turning into three
multipart ones.

The cost is that a path is now client-supplied when the row is written, which
is why `attach_audio_note` below exists and why every caller must use it.
"""

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.config import get_settings
from app.dependencies import CurrentUser, DbSession  # noqa: F401  (DbSession kept for symmetry)
from app.models.user import User
from app.schemas.media import UploadedMedia
from app.services.storage import (
    StorageError,
    UnsupportedMedia,
    is_owned_by,
    store_upload,
)

router = APIRouter(prefix="/media", tags=["media"])
settings = get_settings()


def attach_audio_note(path: str | None, user: User) -> str | None:
    """Validate a client-supplied audio path before it goes in a row.

    Without this check a caller could attach any recording they can name —
    including another worker's dispute — simply by sending its path. The key
    encodes who uploaded it; this is the only thing that verifies it.

    Returns the path unchanged, or None when nothing was sent.

    Raises:
        HTTPException: 403 when the path was not uploaded by `user`.
    """
    if not path:
        return None
    if not is_owned_by(path, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="That recording was not uploaded by this account",
        )
    return path


@router.post(
    "/audio-notes",
    response_model=UploadedMedia,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a short voice note and get back a path to attach",
)
async def upload_audio_note(
    user: CurrentUser,
    audio: UploadFile = File(..., description="Short recording, typically .m4a"),
) -> UploadedMedia:
    """Store one recording.

    Open to workers as well as contractors: a worker records the reason for an
    advance request, or what is wrong with an attendance row.

    The response `path` is what to send back when attaching. Treat it as
    opaque — it encodes the uploader, and the attach step checks that.
    """
    payload = await audio.read()
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The recording was empty"
        )
    if len(payload) > settings.max_audio_note_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                "Voice notes must be under "
                f"{settings.max_audio_note_bytes // (1024 * 1024)} MB. "
                "Record a shorter one."
            ),
        )

    try:
        stored = store_upload(
            payload,
            kind="audio",
            owner_id=user.id,
            content_type=audio.content_type,
        )
    except UnsupportedMedia as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return UploadedMedia(
        path=stored.path,
        url=stored.url,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
    )
