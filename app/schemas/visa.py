import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import (
    ApplicationStatus,
    CaseFileKind,
    CaseStage,
    DocumentStatus,
    InvoiceKind,
    InvoiceStatus,
)
from app.schemas.common import ORMModel


class RequirementOut(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_mandatory: bool
    is_intake: bool = False  # passport / visa grant: can be uploaded before the contract
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
    locked: bool = False  # unlocks after the signed contract and deposit are approved


class Progress(BaseModel):
    required: int  # mandatory requirements
    uploaded: int  # mandatory requirements with a non-rejected upload
    approved: int  # mandatory requirements with an approved upload


class CustomerBrief(ORMModel):
    id: uuid.UUID
    full_name: str
    email: str
    phone: str | None


class StaffBriefOut(ORMModel):
    id: uuid.UUID
    full_name: str
    email: str


class ApplicationSummary(BaseModel):
    id: uuid.UUID
    ref_no: str | None = None
    stage: CaseStage = CaseStage.INTAKE
    # Who has to act next: customer, admin (assign a staff member), staff, or none.
    waiting_on: Literal["customer", "admin", "staff", "none"] = "customer"
    stage_changed_at: datetime | None = None
    assigned_staff: StaffBriefOut | None = None
    visa_type: VisaTypeBrief
    destination_country: str
    status: ApplicationStatus
    customer: CustomerBrief
    assigned_staff_id: uuid.UUID | None
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    progress: Progress


class CaseFileOut(ORMModel):
    id: uuid.UUID
    kind: CaseFileKind
    file_name: str
    size_bytes: int
    signer_name: str | None
    signed_at: datetime | None
    recalled_at: datetime | None
    recall_reason: str | None
    created_at: datetime


class InvoiceOut(ORMModel):
    id: uuid.UUID
    kind: InvoiceKind
    number: str
    amount_cents: int
    currency: str
    description: str
    status: InvoiceStatus
    paid_at: datetime | None
    created_at: datetime


class ApplicationDetail(ApplicationSummary):
    checklist: list[ChecklistItem]
    other_documents: list[DocumentOut]
    documents_unlocked: bool = False
    files: list[CaseFileOut] = []  # newest first
    invoices: list[InvoiceOut] = []  # newest first


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


# ---------------------------------------------------------------- case steps


class AssignIn(BaseModel):
    staff_id: uuid.UUID


class SignIn(BaseModel):
    kind: Literal[CaseFileKind.CONTRACT, CaseFileKind.DATA_CONFIRMATION]
    signer_name: str = Field(min_length=2, max_length=200)
    # PNG of the drawn signature, base64 (a "data:image/png;base64," prefix is fine).
    signature_png: str = Field(min_length=100, max_length=3_000_000)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class AmountIn(BaseModel):
    amount: str = Field(min_length=1, max_length=20, description="Dollars, e.g. 1800 or 1800.50")


class CheckoutOut(BaseModel):
    url: str  # open in a browser tab; the customer returns to the app afterwards
