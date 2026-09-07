from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.models.enums import UserRole
from app.schemas.user import Name, Password, Phone, UserRead


class RegisterRequest(BaseModel):
    name: Name
    phone: Phone
    password: Password
    role: UserRole
    # Workers only: their agreed day rate and the contractor they work for.
    daily_wage: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    contractor_id: Optional[int] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_role_fields(self) -> "RegisterRequest":
        if self.role is UserRole.CONTRACTOR and (
            self.daily_wage is not None or self.contractor_id is not None
        ):
            raise ValueError("contractors cannot have a daily_wage or a contractor_id")
        return self


class LoginRequest(BaseModel):
    phone: Phone
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until the token expires
    user: UserRead
