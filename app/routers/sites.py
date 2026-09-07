"""Sites: the jobs a contractor is running, and what happened on them.

Three things live here, and they are together because they all key off one
site the caller must be proved to own:

  * the sites themselves — list, create, edit, close
  * the daily photo feed
  * the rain-out switch, which marks the whole site's crew at once
"""

from datetime import date as date_type
from typing import Optional

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.dependencies import (
    CurrentContractor,
    CurrentSiteStaff,
    DbSession,
    accessible_site_ids,
    require_site_access,
)
from app.models.attendance import Attendance
from app.models.enums import AttendanceStatus, UserRole
from app.models.site import Site
from app.models.site_photo import SitePhoto
from app.models.user import User
from app.schemas.attendance import AttendanceRead
from app.schemas.site import (
    RainOutResult,
    SiteCreate,
    SitePhotoRead,
    SiteRead,
    SiteUpdate,
)
from app.services.storage import (
    StorageError,
    UnsupportedMedia,
    delete_stored,
    public_url,
    store_upload,
)

router = APIRouter(prefix="/contractor/sites", tags=["sites"])
settings = get_settings()


def _reachable(db: Session, staff: User, site_id: int) -> Site:
    """The site, if this admin owns it or this supervisor is assigned to it.

    Delegates to the shared check in `app.dependencies` rather than testing
    `contractor_id` here, so the two roles cannot drift apart: a supervisor
    passes on `supervisor_id` and on nothing else. Someone else's site is a
    404, matching the rest of the contractor API.
    """
    return require_site_access(db, staff, site_id)


def _check_supervisor(db: Session, contractor: User, supervisor_id: Optional[int]) -> None:
    """Refuse a supervisor who is not this contractor's to assign.

    `sites.supervisor_id` is a plain FK to `users`, which cannot express "and
    their role is supervisor" or "and they work for you". Both are checked
    here, because without them an admin could hand their site to any account
    id they cared to guess — including a worker, whose app would then have two
    contradictory ideas about what they are.

    None clears the assignment, which is how a site goes back to being run by
    the admin directly.
    """
    if supervisor_id is None:
        return

    supervisor = db.get(User, supervisor_id)
    if (
        supervisor is None
        or supervisor.role is not UserRole.SUPERVISOR
        or supervisor.contractor_id != contractor.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Supervisor not found"
        )


def _read_site(site: Site, workers: int = 0, photos: int = 0) -> SiteRead:
    return SiteRead(
        id=site.id,
        name=site.name,
        address=site.address,
        is_active=site.is_active,
        created_at=site.created_at,
        updated_at=site.updated_at,
        worker_count=workers,
        photo_count=photos,
        supervisor_id=site.supervisor_id,
        supervisor_name=site.supervisor.name if site.supervisor else None,
    )


def _read_photo(photo: SitePhoto) -> SitePhotoRead:
    return SitePhotoRead(
        id=photo.id,
        site_id=photo.site_id,
        date=photo.date,
        description=photo.description,
        uploaded_by=photo.uploaded_by,
        uploader_name=photo.uploader.name if photo.uploader else None,
        content_type=photo.content_type,
        size_bytes=photo.size_bytes,
        created_at=photo.created_at,
        # public_url never returns None for a non-empty path, which file_path
        # always is (the column is NOT NULL).
        url=public_url(photo.file_path) or "",
    )


# --- the sites themselves ---------------------------------------------------


@router.get("", response_model=list[SiteRead])
def list_sites(
    staff: CurrentSiteStaff,
    db: DbSession,
    include_inactive: bool = Query(default=False),
) -> list[SiteRead]:
    """The sites this caller may act on, with the counts the list screen shows.

    An admin gets every site they own. A supervisor gets the ones assigned to
    them — usually exactly one, which is why their dashboard can open straight
    onto it instead of asking them to choose.

    The two counts are aggregated in one grouped query each rather than per
    row, so adding a tenth site does not add ten round trips.
    """
    reachable = accessible_site_ids(db, staff)
    if not reachable:
        return []

    stmt = select(Site).where(Site.id.in_(reachable))
    if not include_inactive:
        stmt = stmt.where(Site.is_active.is_(True))
    sites = list(db.scalars(stmt.order_by(Site.is_active.desc(), Site.name)).all())
    if not sites:
        return []

    site_ids = [site.id for site in sites]

    worker_counts = dict(
        db.execute(
            select(User.site_id, func.count())
            .where(
                User.site_id.in_(site_ids),
                User.role == UserRole.WORKER,
                User.is_active.is_(True),
            )
            .group_by(User.site_id)
        ).all()
    )
    photo_counts = dict(
        db.execute(
            select(SitePhoto.site_id, func.count())
            .where(SitePhoto.site_id.in_(site_ids))
            .group_by(SitePhoto.site_id)
        ).all()
    )

    return [
        _read_site(site, worker_counts.get(site.id, 0), photo_counts.get(site.id, 0))
        for site in sites
    ]


@router.post("", response_model=SiteRead, status_code=status.HTTP_201_CREATED)
def add_site(payload: SiteCreate, contractor: CurrentContractor, db: DbSession) -> SiteRead:
    site = Site(
        contractor_id=contractor.id, name=payload.name, address=payload.address
    )
    db.add(site)
    try:
        db.commit()
    except IntegrityError:
        # uq_sites_contractor_name — two "Udaipur Villa" under one contractor
        # would be indistinguishable in every picker.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a site with this name",
        ) from None
    db.refresh(site)
    return _read_site(site)


@router.get("/{site_id}", response_model=SiteRead)
def read_site(site_id: int, staff: CurrentSiteStaff, db: DbSession) -> SiteRead:
    site = _reachable(db, staff, site_id)
    workers = db.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.site_id == site.id,
            User.role == UserRole.WORKER,
            User.is_active.is_(True),
        )
    )
    photos = db.scalar(
        select(func.count()).select_from(SitePhoto).where(SitePhoto.site_id == site.id)
    )
    return _read_site(site, workers or 0, photos or 0)


@router.patch("/{site_id}", response_model=SiteRead)
def edit_site(
    site_id: int, payload: SiteUpdate, contractor: CurrentContractor, db: DbSession
) -> SiteRead:
    """Admin only — including who supervises the site.

    A supervisor may run a site but may not decide which site they run, so
    this keeps `CurrentContractor` while the reads above widened.
    """
    site = _reachable(db, contractor, site_id)

    fields = payload.model_dump(exclude_unset=True)
    if "supervisor_id" in fields:
        _check_supervisor(db, contractor, fields["supervisor_id"])

    for field, value in fields.items():
        setattr(site, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a site with this name",
        ) from None
    db.refresh(site)
    return _read_site(site)


# --- the daily photo feed ---------------------------------------------------


@router.get("/{site_id}/photos", response_model=list[SitePhotoRead])
def list_site_photos(
    site_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[SitePhotoRead]:
    """The site's photos, newest day first."""
    _reachable(db, staff, site_id)

    stmt = (
        select(SitePhoto)
        .options(joinedload(SitePhoto.uploader))
        .where(SitePhoto.site_id == site_id)
    )
    if start_date is not None:
        stmt = stmt.where(SitePhoto.date >= start_date)
    if end_date is not None:
        stmt = stmt.where(SitePhoto.date <= end_date)

    photos = db.scalars(
        stmt.order_by(SitePhoto.date.desc(), SitePhoto.id.desc()).limit(limit)
    ).all()
    return [_read_photo(photo) for photo in photos]


@router.post(
    "/{site_id}/photos",
    response_model=SitePhotoRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload one progress photo or material receipt",
)
async def upload_site_photo(
    site_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
    image: UploadFile = File(..., description="JPEG, PNG, WebP or HEIC"),
    date: Optional[date_type] = Form(
        default=None, description="The day the photo is about. Defaults to today."
    ),
    description: Optional[str] = Form(default=None, max_length=255),
) -> SitePhotoRead:
    """Store one image against a site and a date.

    The file is written to storage BEFORE the row is inserted, so a failed
    write never leaves a row pointing at nothing. The reverse — a stored file
    whose insert then fails — leaves an orphan on disk, which is the cheaper
    of the two failures and is cleaned up here on the error path.
    """
    site = _reachable(db, staff, site_id)

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

    when = date or date_type.today()
    if when > date_type.today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A photo cannot be dated in the future",
        )

    try:
        stored = store_upload(
            payload,
            kind="photos",
            owner_id=staff.id,
            content_type=image.content_type,
            label=description or site.name,
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
        site_id=site.id,
        file_path=stored.path,
        date=when,
        description=(description or "").strip() or None,
        uploaded_by=staff.id,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
    )
    db.add(photo)
    try:
        db.commit()
    except Exception:
        db.rollback()
        # Do not leave the bytes behind for a row that does not exist.
        delete_stored(stored.path)
        raise

    db.refresh(photo)
    return _read_photo(photo)


@router.delete("/{site_id}/photos/{photo_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_site_photo(
    site_id: int, photo_id: int, staff: CurrentSiteStaff, db: DbSession
) -> Response:
    _reachable(db, staff, site_id)

    photo = db.get(SitePhoto, photo_id)
    if photo is None or photo.site_id != site_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

    path = photo.file_path
    db.delete(photo)
    db.commit()
    # After the commit: a delete that fails here leaves an orphaned file, which
    # is recoverable. Deleting first and then failing to commit would leave a
    # row pointing at a missing image, which is not.
    delete_stored(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- rain-out ---------------------------------------------------------------


@router.post("/{site_id}/rainout", response_model=RainOutResult)
def mark_site_rained_out(
    site_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
    date: Optional[date_type] = Query(default=None, description="Defaults to today"),
    overwrite: bool = Query(
        default=True,
        description="Replace statuses already recorded for the day. False skips them.",
    ),
) -> RainOutResult:
    """Mark every active worker on this site as rained out for the day.

    One statement of fact — it rained, nobody worked — applied across the
    site's crew, because making the contractor tap twelve rows in the wet is
    the reason this endpoint exists.

    Overwrites by default, and says how many rows it overwrote. A worker
    already marked present who is then rained out is the normal case: they
    turned up, the sky opened, and the earlier mark is now wrong. Pass
    `overwrite=false` to leave existing marks alone.
    """
    site = _reachable(db, staff, site_id)
    when = date or date_type.today()
    if when > date_type.today():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A day cannot be rained out before it happens",
        )

    # Scoped through the SITE's owner rather than the caller's id: a
    # supervisor does not own the crew, the admin above them does, and
    # comparing against `staff.id` would find nobody.
    crew = list(
        db.scalars(
            select(User).where(
                User.site_id == site.id,
                User.contractor_id == site.contractor_id,
                User.role == UserRole.WORKER,
                User.is_active.is_(True),
            )
        ).all()
    )
    if not crew:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No active workers are assigned to {site.name}. "
                "Assign the crew to this site first."
            ),
        )

    worker_ids = [worker.id for worker in crew]
    existing = {
        row.worker_id: row
        for row in db.scalars(
            select(Attendance).where(
                Attendance.worker_id.in_(worker_ids), Attendance.date == when
            )
        ).all()
    }

    created = updated = unchanged = 0
    for worker in crew:
        row = existing.get(worker.id)
        if row is None:
            db.add(
                Attendance(
                    worker_id=worker.id,
                    date=when,
                    site_id=site.id,
                    status=AttendanceStatus.RAIN_OUT,
                )
            )
            created += 1
        elif row.status is AttendanceStatus.RAIN_OUT:
            unchanged += 1
        elif overwrite:
            row.status = AttendanceStatus.RAIN_OUT
            if row.site_id is None:
                row.site_id = site.id
            updated += 1
        else:
            unchanged += 1

    db.commit()

    return RainOutResult(
        site_id=site.id,
        site_name=site.name,
        date=when,
        created=created,
        updated=updated,
        unchanged=unchanged,
        worker_ids=worker_ids,
    )


@router.get("/{site_id}/attendance", response_model=list[AttendanceRead])
def read_site_attendance(
    site_id: int,
    staff: CurrentSiteStaff,
    db: DbSession,
    date: Optional[date_type] = Query(default=None, description="Defaults to today"),
) -> list[Attendance]:
    """One day's attendance for the crew posted to this site.

    Lets the app show the grid straight after a rain-out without refetching
    every worker's whole ledger.
    """
    site = _reachable(db, staff, site_id)
    when = date or date_type.today()

    return list(
        db.scalars(
            select(Attendance)
            .join(User, User.id == Attendance.worker_id)
            .where(
                Attendance.date == when,
                User.site_id == site.id,
                User.contractor_id == site.contractor_id,
            )
            .order_by(Attendance.worker_id)
        ).all()
    )
