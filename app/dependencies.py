"""Shared FastAPI dependencies: the DB session, the caller, and ownership."""

from typing import TYPE_CHECKING, Annotated, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.database import get_db
from app.models.enums import UserRole
from app.models.user import User

if TYPE_CHECKING:
    from app.models.site import Site

# auto_error=False so a missing header returns 401 with our own message
# instead of the default 403.
bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]

_credentials_error = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    db: DbSession,
    credentials: Annotated[
        Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)
    ],
) -> User:
    if credentials is None:
        raise _credentials_error

    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise _credentials_error from None

    user = db.get(User, user_id)
    if user is None:
        raise _credentials_error
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated",
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def _require_role(user: User, role: UserRole) -> User:
    if user.role is not role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This endpoint is only available to accounts with the {role.value} role",
        )
    return user


def get_current_contractor(user: CurrentUser) -> User:
    """Admin only.

    Named `contractor` because that is what the role was called and what every
    caller still says; `UserRole.CONTRACTOR` is an alias of `ADMIN`. A
    SUPERVISOR is deliberately NOT admitted here — this dependency guards the
    money, and the whole point of the role is that it does not see it.
    """
    return _require_role(user, UserRole.ADMIN)


def get_current_worker(user: CurrentUser) -> User:
    return _require_role(user, UserRole.WORKER)


def get_current_site_staff(user: CurrentUser) -> User:
    """Admin or supervisor — anyone who may run a site.

    The two roles are separated afterwards by `require_site_access`, which is
    where "their assigned site" is enforced. Splitting it this way keeps the
    role check and the scope check in different places: one answers "may you
    touch sites at all", the other "may you touch THIS one".
    """
    if user.role not in (UserRole.ADMIN, UserRole.SUPERVISOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is only available to admins and supervisors",
        )
    return user


CurrentContractor = Annotated[User, Depends(get_current_contractor)]
CurrentWorker = Annotated[User, Depends(get_current_worker)]
CurrentSiteStaff = Annotated[User, Depends(get_current_site_staff)]


# --- ownership ---------------------------------------------------------------
#
# Every contractor-scoped route has to prove the thing it is about belongs to
# the caller before it touches it. These two are the check, and they answer
# 404 rather than 403 on purpose: a 403 would confirm that somebody else's
# site or worker exists, which is more than an unrelated contractor should be
# able to learn by guessing ids.
#
# `contractor.py` and `payroll.py` predate these and carry their own copies;
# new routers should use these rather than growing a third.


def require_own_site(db: Session, contractor: User, site_id: int) -> "Site":
    from app.models.site import Site

    site = db.get(Site, site_id)
    if site is None or site.contractor_id != contractor.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Site not found"
        )
    return site


def require_site_access(db: Session, staff: User, site_id: int) -> "Site":
    """Prove this admin or supervisor may act on this site.

    An ADMIN owns sites, so ownership is the test. A SUPERVISOR owns nothing —
    they are assigned, and the test is `sites.supervisor_id`. One supervisor
    posting attendance to another supervisor's site is the exact hole this
    closes.

    404 rather than 403 on a miss, for the same reason as the two checks below:
    a 403 would confirm that somebody else's site exists.
    """
    from app.models.site import Site

    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Site not found"
        )

    if staff.role is UserRole.ADMIN:
        permitted = site.contractor_id == staff.id
    else:
        permitted = site.supervisor_id == staff.id

    if not permitted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Site not found"
        )
    return site


def accessible_site_ids(db: Session, staff: User) -> list[int]:
    """Every site this admin or supervisor may act on.

    The list form of `require_site_access`, for the endpoints that answer
    "all of them" rather than "this one": an admin owns sites, a supervisor is
    assigned them. Returned as ids rather than rows because every caller wants
    it for an `IN (...)`.
    """
    from app.models.site import Site

    column = (
        Site.contractor_id if staff.role is UserRole.ADMIN else Site.supervisor_id
    )
    return list(db.scalars(select(Site.id).where(column == staff.id)).all())


def require_site_worker(db: Session, staff: User, worker_id: int) -> User:
    """A worker this admin or supervisor may act on.

    An admin's crew is everyone carrying their contractor_id. A supervisor's is
    narrower and derived rather than stored: only the workers actually posted
    to a site they run. That is what stops one supervisor marking attendance
    for, or checking a tool out against, another site's crew.

    404 rather than 403 on a miss, for the same reason as the checks above.
    """
    worker = db.get(User, worker_id)
    if worker is None or worker.role is not UserRole.WORKER:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found"
        )

    if staff.role is UserRole.ADMIN:
        permitted = worker.contractor_id == staff.id
    else:
        permitted = worker.site_id is not None and worker.site_id in set(
            accessible_site_ids(db, staff)
        )

    if not permitted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found"
        )
    return worker


def require_own_worker(db: Session, contractor: User, worker_id: int) -> User:
    worker = db.get(User, worker_id)
    if (
        worker is None
        or worker.role is not UserRole.WORKER
        or worker.contractor_id != contractor.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found"
        )
    return worker
