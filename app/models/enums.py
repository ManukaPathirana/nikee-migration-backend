from enum import StrEnum


class UserRole(StrEnum):
    SUPER_ADMIN = "super_admin"  # all teams + oversight
    ADMIN = "admin"  # team admin: one team (visa type)
    STAFF = "staff"  # team member: one team (visa type)
    CUSTOMER = "customer"


class VerificationPurpose(StrEnum):
    VERIFY = "verify"
    RESET = "reset"
    INVITE = "invite"


class InquiryStatus(StrEnum):
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MeetingStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    RESCHEDULE_REQUESTED = "reschedule_requested"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MeetingMode(StrEnum):
    ONLINE = "online"
    IN_PERSON = "in_person"
    PHONE = "phone"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class CaseStage(StrEnum):
    """Where a visa case is in the Nikee process (stored as text on visa_applications.stage)."""

    INTAKE = "intake"  # customer adds passport + visa grant, then submits
    ASSIGNMENT = "assignment"  # team admin picks a staff member
    CONTRACT_PENDING = "contract_pending"  # staff prepares contract + deposit invoice
    AGREEMENT = "agreement"  # customer signs the contract and pays the deposit
    AGREEMENT_REVIEW = "agreement_review"  # staff checks signed contract + deposit
    DOCUMENTS = "documents"  # full checklist unlocked; staff reviews, then finalizes
    FINAL_PAYMENT = "final_payment"  # customer pays the final invoice
    CONFIRMATION_PENDING = "confirmation_pending"  # staff sends final application + confirmation
    CONFIRMATION = "confirmation"  # customer signs the data confirmation
    CONFIRMATION_REVIEW = "confirmation_review"  # staff checks the signed confirmation
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"


class CaseFileKind(StrEnum):
    CONTRACT = "contract"
    SIGNED_CONTRACT = "signed_contract"
    FINAL_APPLICATION = "final_application"
    DATA_CONFIRMATION = "data_confirmation"
    SIGNED_DATA_CONFIRMATION = "signed_data_confirmation"


class InvoiceKind(StrEnum):
    DEPOSIT = "deposit"
    FINAL = "final"


class InvoiceStatus(StrEnum):
    UNPAID = "unpaid"
    PAID = "paid"
    VOID = "void"
