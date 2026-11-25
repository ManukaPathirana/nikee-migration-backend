import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, model_validator

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
    # Set when the staff member or team admin requested it for this case only.
    application_id: uuid.UUID | None = None


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

    @model_validator(mode="wrap")
    @classmethod
    def _team_admin_for_customers(cls, data: Any, handler: Any) -> "StaffBriefOut":
        """Customers see their team admin, not the staff member handling the case."""
        from app.core.viewer import public_person, viewer_is_customer

        if viewer_is_customer():
            contact = public_person(data)
            if contact is not None:
                return cls(id=contact.id, full_name=contact.full_name, email=contact.email)
        return handler(data)


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
    payment_type: str | None = None
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


class CaseStartIn(BaseModel):
    """Team admin: open a visa case for a customer. Give customer_id (existing account), or the
    new customer's email and name (they get an email to sign up)."""

    customer_id: uuid.UUID | None = None
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    visa_type_id: uuid.UUID | None = None  # team admins: their own team (super admin: required)
    destination_country: str = Field(default="Australia", min_length=2, max_length=100)
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _who(self) -> "CaseStartIn":
        if self.customer_id is None and self.email is None:
            raise ValueError("Choose a customer or enter the new customer's email")
        if self.customer_id is None and len((self.full_name or "").strip()) < 2:
            raise ValueError("Enter the new customer's name")
        return self


class DocumentRequestIn(BaseModel):
    """Staff / team admin: ask the customer for a document that isn't in the visa type's list."""

    name: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    is_mandatory: bool = True


class CustomerPick(ORMModel):
    id: uuid.UUID
    full_name: str
    email: str
    phone: str | None = None
    is_verified: bool


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
    payment_type: str = Field(
        "", max_length=120, description="What the payment is for; empty = the default label"
    )


class ChangesIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class CheckoutIn(BaseModel):
    payment_type: str = Field(
        "", max_length=120, description="What the customer is paying for; empty = keep it"
    )


class CheckoutOut(BaseModel):
    url: str  # open in a browser tab; the customer returns to the app afterwards
