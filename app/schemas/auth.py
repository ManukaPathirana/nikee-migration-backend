from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, Field

from app.schemas.user import UserOut


def _strong_password(value: str) -> str:
    if not any(c.isalpha() for c in value) or not any(c.isdigit() for c in value):
        raise ValueError("Password must contain at least one letter and one number")
    return value


Password = Annotated[str, Field(min_length=8, max_length=128), AfterValidator(_strong_password)]
OtpCode = Annotated[str, Field(pattern=r"^\d{6}$")]


class RegisterIn(BaseModel):
    email: EmailStr
    password: Password
    full_name: str = Field(min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=40)


class EmailIn(BaseModel):
    email: EmailStr


class VerifyEmailIn(BaseModel):
    email: EmailStr
    code: OtpCode


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=200)


class ResetPasswordIn(BaseModel):
    email: EmailStr
    code: OtpCode
    new_password: Password


class AcceptInviteIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: Password


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: Password


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
