from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.security import create_access_token, hash_password, verify_password
from app.dependencies import CurrentUser, DbSession
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.schemas.user import UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(user: User) -> TokenResponse:
    token, expires_in = create_access_token(user.id, user.role)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserRead.model_validate(user),
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: DbSession) -> TokenResponse:
    """Create an account and hand back a token, so the app can go straight in.

    Contractors sign themselves up here; a worker can also self-register, but
    the usual path is a contractor adding them via POST /contractor/workers.
    """
    if db.scalar(select(User).where(User.phone == payload.phone)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this phone number already exists",
        )

    if payload.role is UserRole.WORKER and payload.contractor_id is not None:
        # A worker joins a crew because the contractor adds them (POST
        # /contractor/workers), never by naming one. Accepting the id here let
        # anybody who guessed a contractor's number put themselves on that
        # crew - into the attendance grid, the payroll, the cash totals. The
        # app never sends it for a worker; only a direct call could.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Workers are added to a crew by their contractor, not at sign-up",
        )

    if payload.contractor_id is not None:
        contractor = db.get(User, payload.contractor_id)
        if contractor is None or contractor.role is not UserRole.CONTRACTOR:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Contractor not found"
            )

    user = User(
        name=payload.name,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=payload.role,
        daily_wage=payload.daily_wage,
        contractor_id=payload.contractor_id,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Another request claimed the same phone number in between.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this phone number already exists",
        ) from None
    db.refresh(user)

    return _token_response(user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    user = db.scalar(select(User).where(User.phone == payload.phone))
    # Same message either way, so the endpoint does not reveal which phone
    # numbers are registered.
    #
    # `password_hash` is nullable: a worker booked onto the crew without a PIN
    # has no digest, and is refused here rather than being passed to bcrypt.
    # Checked before `verify_password` and folded into the same message, so a
    # missing PIN is not distinguishable from a wrong one.
    if (
        user is None
        or user.password_hash is None
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect phone number or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated",
        )

    return _token_response(user)


@router.get("/me", response_model=UserRead)
def read_current_user(user: CurrentUser) -> User:
    """Who the bearer token belongs to — used by the app on cold start."""
    return user
