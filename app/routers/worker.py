from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.core.clock import business_today
from app.config import get_settings
from app.dependencies import CurrentWorker, DbSession
from app.models.advance_request import AdvanceRequest
from app.models.attendance import Attendance
from app.models.enums import AdvanceRequestStatus
from app.models.site_photo import SitePhoto
from app.models.user import User
from app.routers.media import attach_audio_note
from app.routers.sites import _read_photo
from app.schemas.advance_request import AdvanceRequestCreate, AdvanceRequestRead
from app.schemas.attendance import AttendanceRead, DisputeRequest
from app.schemas.ledger import WorkerLedger
from app.schemas.site import SitePhotoRead
from app.schemas.user import WorkerProfile
from app.services.ledger import build_worker_ledger
from app.services.storage import (
    StorageError,
    UnsupportedMedia,
    delete_stored,
    public_url,
    store_upload,
)

settings = get_settings()

router = APIRouter(prefix="/worker", tags=["worker"])

# How many requests a worker may have open at once. One is the honest number:
# a queue of pending asks is a conversation to have in person, not a list for
# the contractor to work through.
MAX_OPEN_REQUESTS = 1


@router.get("/me", response_model=WorkerProfile)
def read_my_profile(worker: CurrentWorker, db: DbSession) -> User:
    """The worker's own record, including the site they are posted to.

    The dashboard needs three things from one call: the id to put in the QR
    code, the site name to show, and the address to hand to Google Maps.
    """
    # `site` is a lazy relationship the response serialises, so it is loaded
    # up front rather than left to fire a second query during serialisation.
    return db.scalar(
        select(User).options(joinedload(User.site)).where(User.id == worker.id)
    )


@router.get("/ledger", response_model=WorkerLedger)
def read_my_ledger(
    worker: CurrentWorker,
    db: DbSession,
    start_date: Optional[date_type] = Query(
        default=None, description="Only list rows on or after this date"
    ),
    end_date: Optional[date_type] = Query(
        default=None, description="Only list rows on or before this date"
    ),
) -> WorkerLedger:
    """The signed-in worker's own transactions, attendance and balance.

    The date filters narrow the listed rows; the balance always covers the
    worker's full history.
    """
    return build_worker_ledger(db, worker, start_date, end_date)


# --- advances ---------------------------------------------------------------


def _read_request(request: AdvanceRequest) -> AdvanceRequestRead:
    """Attach the playable URL, which is derived rather than stored."""
    read = AdvanceRequestRead.model_validate(request)
    read.audio_note_url = public_url(request.audio_note_path)
    return read


@router.post(
    "/advance-requests",
    response_model=AdvanceRequestRead,
    status_code=status.HTTP_201_CREATED,
)
def request_advance(
    payload: AdvanceRequestCreate, worker: CurrentWorker, db: DbSession
) -> AdvanceRequestRead:
    """Ask the contractor for an advance.

    Nothing reaches the ledger here — the amount only becomes a transaction if
    and when the contractor approves it.
    """
    if worker.contractor_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are not linked to a contractor yet, so there is nobody to ask",
        )

    open_count = db.scalar(
        select(AdvanceRequest.id)
        .where(
            AdvanceRequest.worker_id == worker.id,
            AdvanceRequest.status == AdvanceRequestStatus.PENDING,
        )
        .limit(MAX_OPEN_REQUESTS)
    )
    if open_count is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a request waiting for an answer",
        )

    request = AdvanceRequest(
        worker_id=worker.id,
        amount=payload.amount,
        reason=payload.reason,
        status=AdvanceRequestStatus.PENDING,
        audio_note_path=attach_audio_note(payload.audio_note_path, worker),
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return _read_request(request)


@router.get("/advance-requests", response_model=list[AdvanceRequestRead])
def list_my_advance_requests(
    worker: CurrentWorker,
    db: DbSession,
    status_filter: Optional[AdvanceRequestStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AdvanceRequestRead]:
    """The worker's own requests, newest first, so they can see the answer."""
    stmt = select(AdvanceRequest).where(AdvanceRequest.worker_id == worker.id)
    if status_filter is not None:
        stmt = stmt.where(AdvanceRequest.status == status_filter)
    requests = db.scalars(
        stmt.order_by(AdvanceRequest.created_at.desc()).limit(limit)
    ).all()
    return [_read_request(request) for request in requests]


# --- disputes ---------------------------------------------------------------


def _read_attendance(attendance: Attendance) -> AttendanceRead:
    read = AttendanceRead.model_validate(attendance)
    read.audio_note_url = public_url(attendance.audio_note_path)
    return read


@router.patch("/attendance/{attendance_id}/dispute", response_model=AttendanceRead)
def flag_attendance(
    attendance_id: int,
    worker: CurrentWorker,
    db: DbSession,
    payload: Optional[DisputeRequest] = None,
    disputed: Optional[bool] = Query(
        default=None,
        description="Legacy query form. The body is preferred; it can carry a voice note.",
    ),
) -> AttendanceRead:
    """Flag one of the worker's own attendance rows as wrong.

    The row itself is not changed — a worker cannot rewrite their own
    attendance, only say that it does not match the day they worked. The
    contractor sees the flag and settles it in person.
    """
    attendance = db.get(Attendance, attendance_id)
    # Someone else's row 404s rather than 403s, so this cannot be used to
    # probe which attendance ids exist.
    if attendance is None or attendance.worker_id != worker.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found"
        )

    body = payload or DisputeRequest()
    # The query parameter still wins when explicitly sent, so a client written
    # against the Phase 6 signature keeps working.
    attendance.is_disputed = disputed if disputed is not None else body.disputed
    if body.audio_note_path:
        attendance.audio_note_path = attach_audio_note(body.audio_note_path, worker)

    db.commit()
    db.refresh(attendance)
    return _read_attendance(attendance)


# --- site snaps -------------------------------------------------------------


@router.get("/site-photos", response_model=list[SitePhotoRead])
def list_my_site_photos(
    worker: CurrentWorker,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SitePhotoRead]:
    """The feed for the site this worker is posted to.

    Empty rather than a 404 when they are not posted anywhere: an unassigned
    worker has no site to have photographed, which is a state to render, not
    an error to raise.
    """
    if worker.site_id is None:
        return []

    photos = db.scalars(
        select(SitePhoto)
        .options(joinedload(SitePhoto.uploader))
        .where(SitePhoto.site_id == worker.site_id)
        .order_by(SitePhoto.date.desc(), SitePhoto.id.desc())
        .limit(limit)
    ).all()
    return [_read_photo(photo) for photo in photos]


@router.post(
    "/site-photos",
    response_model=SitePhotoRead,
    status_code=status.HTTP_201_CREATED,
    summary="Post one progress photo to the site this worker is posted to",
)
async def upload_my_site_photo(
    worker: CurrentWorker,
    db: DbSession,
    image: UploadFile = File(..., description="JPEG, PNG, WebP or HEIC"),
    date: Optional[date_type] = Form(
        default=None, description="The day the photo is about. Defaults to today."
    ),
    description: Optional[str] = Form(default=None, max_length=255),
) -> SitePhotoRead:
    """A worker photographing the day's progress on their own site.

    THE SITE IS NOT A PARAMETER
    ---------------------------
    The contractor's version of this takes `site_id` from the path and proves
    ownership. This one reads it off `worker.site_id` and offers no way to
    override it. That is the whole access control: a worker cannot name a
    site, so they cannot post to one they are not standing on, and no amount
    of tampering with the request body changes which site the row lands
    against.

    A worker posted nowhere gets a 409 rather than a silent success — there is
    no site for the photo to belong to, and storing it against nothing would
    lose it.

    File first, row second, orphan cleaned up on failure — same ordering and
    same reasoning as the contractor's upload.
    """
    if worker.site_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "You are not posted to a site yet, so there is nowhere to file this "
                "photo. Ask your contractor to assign you to one."
            ),
        )

    payload = await image.read()
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The image was empty"
        )
    if len(payload) > settings.max_image_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Images must be under {settings.max_image_bytes // (1024 * 1024)} MB",
        )

    when = date or business_today()
    if when > business_today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A photo cannot be dated in the future",
        )

    try:
        stored = store_upload(
            payload,
            kind="photos",
            # Filed under the uploader, like every other upload, so an orphan
            # sweep can attribute the bytes.
            owner_id=worker.id,
            content_type=image.content_type,
            label=description or "site snap",
        )
    except UnsupportedMedia as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    photo = SitePhoto(
        site_id=worker.site_id,
        file_path=stored.path,
        date=when,
        description=(description or "").strip() or None,
        uploaded_by=worker.id,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
    )
    db.add(photo)
    try:
        db.commit()
    except Exception:
        db.rollback()
        delete_stored(stored.path)
        raise

    db.refresh(photo)
    return _read_photo(photo)
