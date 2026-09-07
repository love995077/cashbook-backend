"""Password hashing and JWT access tokens."""

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.config import get_settings
from app.models.enums import UserRole

settings = get_settings()

# bcrypt hashes at most the first 72 bytes of a password.
BCRYPT_MAX_BYTES = 72


def _to_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_to_bytes(password), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bytes(password), password_hash.encode())
    except ValueError:
        # Stored value is not a valid bcrypt hash.
        return False


def create_access_token(user_id: int, role: UserRole) -> tuple[str, int]:
    """Return a signed access token and its lifetime in seconds."""
    expires_in = settings.jwt_expire_minutes * 60
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode a token, raising jwt.InvalidTokenError if it is not usable."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
