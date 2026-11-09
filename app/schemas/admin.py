import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from app.models.enums import ApplicationStatus, InquiryStatus
from app.schemas.common import ORMModel
from app.schemas.inquiry import InquiryOut
from app.schemas.user import CustomerProfileOut
from app.schemas.visa import ApplicationSummary, RequirementOut

# ---------------------------------------------------------------- staff


class StaffCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    designation: str | None = Field(default=None, max_length=120)
    specialization: str | None = Field(default=None, max_length=200)


class StaffUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    designation: str | None = Field(default=None, max_length=120)
    specialization: str | None = Field(default=None, max_length=200)


class StaffOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    phone: str | None
    designation: str | None
    specialization: str | None
    is_active: bool
    invite_pending: bool  # hasn't set a password yet
    open_inquiries: int
    last_login_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------- customers


class CustomerOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    phone: str | None
    is_active: bool
    is_verified: bool
    applications_count: int
    inquiries_count: int
    last_login_at: datetime | None
    created_at: datetime


class CustomerDetail(CustomerOut):
    profile: CustomerProfileOut | None
    applications: list[ApplicationSummary]
    inquiries: list[InquiryOut]


# ---------------------------------------------------------------- dashboard


class WeekCount(BaseModel):
    week_start: datetime
    count: int


class ActivityOut(BaseModel):
    id: uuid.UUID
    actor_name: str | None
    action: str
    entity: str
    entity_id: str | None
    meta: dict[str, Any] | None
    created_at: datetime


class DashboardOut(BaseModel):
    customers_total: int
    customers_new_30d: int
    staff_active: int
    inquiries_by_status: dict[InquiryStatus, int]
    inquiries_unassigned: int
    inquiries_per_week: list[WeekCount]
    applications_by_status: dict[ApplicationStatus, int]
    documents_pending: int
    meetings_upcoming_7d: int
    meetings_awaiting_customer: int
    reviews_count: int
    reviews_average: float | None
    reviews_flagged: int
    recent_activity: list[ActivityOut]


# ---------------------------------------------------------------- visa types


class VisaTypeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=60)
    is_active: bool = True
    sort_order: int = 0


class VisaTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=60)
    is_active: bool | None = None
    sort_order: int | None = None


class VisaTypeAdminOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    icon: str | None
    is_active: bool
    sort_order: int
    requirements: list[RequirementOut]


class RequirementCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    is_mandatory: bool = True
    sort_order: int | None = None


class RequirementUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    is_mandatory: bool | None = None
    sort_order: int | None = None


class ActiveIn(BaseModel):
    is_active: bool
