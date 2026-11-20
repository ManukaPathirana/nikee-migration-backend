import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import InquiryStatus, UserRole
from app.schemas.common import ORMModel

InquiryCategory = Literal[
    "general", "visa_application", "documents", "payment", "appointment", "other"
]


class UserBrief(ORMModel):
    id: uuid.UUID
    full_name: str
    email: str
    role: UserRole

    @model_validator(mode="wrap")
    @classmethod
    def _team_admin_for_customers(cls, data: Any, handler: Any) -> "UserBrief":
        """Customers see the team admin wherever a staff member would appear."""
        from app.core.viewer import public_person, viewer_is_customer

        if viewer_is_customer():
            contact = public_person(data)
            if contact is not None:
                return cls(
                    id=contact.id,
                    full_name=contact.full_name,
                    email=contact.email,
                    role=UserRole.ADMIN,
                )
        return handler(data)


class TeamBrief(ORMModel):
    """A team = a visa type."""

    id: uuid.UUID
    name: str
    slug: str


class InquiryCreate(BaseModel):
    visa_type_id: uuid.UUID  # which team handles it
    subject: str = Field(min_length=3, max_length=200)
    category: InquiryCategory = "general"
    description: str = Field(min_length=10, max_length=5000)
    preferred_contact_time: str | None = Field(default=None, max_length=120)
    application_id: uuid.UUID | None = None


class InquiryOut(ORMModel):
    id: uuid.UUID
    ref_no: str
    visa_type: TeamBrief
    subject: str
    category: str
    description: str
    preferred_contact_time: str | None
    status: InquiryStatus
    customer: UserBrief
    assigned_staff: UserBrief | None
    application_id: uuid.UUID | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class HistoryOut(ORMModel):
    id: uuid.UUID
    from_status: InquiryStatus | None
    to_status: InquiryStatus
    reason: str | None
    changed_at: datetime
    actor: UserBrief | None


class NoteOut(ORMModel):
    id: uuid.UUID
    body: str
    is_internal: bool
    created_at: datetime
    author: UserBrief | None


class InquiryDetail(InquiryOut):
    history: list[HistoryOut]
    notes: list[NoteOut]


class StatusChangeIn(BaseModel):
    status: InquiryStatus
    reason: str | None = Field(default=None, max_length=1000)


class AssignIn(BaseModel):
    staff_id: uuid.UUID


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    # Staff notes are internal unless they choose to share them; customer notes are never internal.
    is_internal: bool = True


class BoardOut(BaseModel):
    columns: dict[InquiryStatus, list[InquiryOut]]
    counts: dict[InquiryStatus, int]


class StaffBrief(ORMModel):
    id: uuid.UUID
    full_name: str
    email: str
    designation: str | None = None
    invite_pending: bool = False  # hasn't accepted the invite yet, can't sign in
    team: TeamBrief | None = None
