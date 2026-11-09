import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import ApplicationStatus, DocumentStatus
from app.schemas.common import ORMModel


class RequirementOut(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_mandatory: bool
    sort_order: int


class VisaTypeBrief(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    icon: str | None


class VisaTypeOut(VisaTypeBrief):
    description: str | None
    sort_order: int
    requirements: list[RequirementOut]


class DocumentOut(ORMModel):
    id: uuid.UUID
    application_id: uuid.UUID
    requirement_id: uuid.UUID | None
    file_name: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    rejection_reason: str | None
    reviewed_at: datetime | None
    created_at: datetime


class ChecklistItem(BaseModel):
    requirement: RequirementOut
    document: DocumentOut | None  # latest upload for this requirement


class Progress(BaseModel):
    required: int  # mandatory requirements
    uploaded: int  # mandatory requirements with a non-rejected upload
    approved: int  # mandatory requirements with an approved upload


class CustomerBrief(ORMModel):
    id: uuid.UUID
    full_name: str
    email: str
    phone: str | None


class ApplicationSummary(BaseModel):
    id: uuid.UUID
    visa_type: VisaTypeBrief
    destination_country: str
    status: ApplicationStatus
    customer: CustomerBrief
    assigned_staff_id: uuid.UUID | None
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    progress: Progress


class ApplicationDetail(ApplicationSummary):
    checklist: list[ChecklistItem]
    other_documents: list[DocumentOut]


class ApplicationCreate(BaseModel):
    visa_type_id: uuid.UUID
    destination_country: str = Field(min_length=2, max_length=100)


class ApplicationStatusIn(BaseModel):
    status: Literal[
        ApplicationStatus.IN_REVIEW, ApplicationStatus.APPROVED, ApplicationStatus.REJECTED
    ]


class DocumentReviewIn(BaseModel):
    status: Literal[DocumentStatus.APPROVED, DocumentStatus.REJECTED]
    rejection_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reason_for_rejection(self) -> "DocumentReviewIn":
        if self.status == DocumentStatus.REJECTED and not (self.rejection_reason or "").strip():
            raise ValueError("A rejection reason is required when rejecting a document")
        return self


class DownloadUrlOut(BaseModel):
    url: str
    expires_in: int
