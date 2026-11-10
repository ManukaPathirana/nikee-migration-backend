import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.enums import UserRole
from app.schemas.common import ORMModel


class CustomerProfileOut(ORMModel):
    dob: date | None = None
    nationality: str | None = None
    passport_no: str | None = None
    address: str | None = None
    country_of_interest: str | None = None


class StaffProfileOut(ORMModel):
    designation: str | None = None
    specialization: str | None = None


class TeamRef(ORMModel):
    id: uuid.UUID
    name: str
    slug: str


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    role: UserRole
    team: TeamRef | None = None  # staff and team admins
    full_name: str
    phone: str | None
    avatar_url: str | None
    is_verified: bool
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class MeOut(UserOut):
    customer_profile: CustomerProfileOut | None = None
    staff_profile: StaffProfileOut | None = None


class CustomerProfileUpdate(BaseModel):
    dob: date | None = None
    nationality: str | None = Field(default=None, max_length=100)
    passport_no: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=1000)
    country_of_interest: str | None = Field(default=None, max_length=100)


class MeUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    customer_profile: CustomerProfileUpdate | None = None
