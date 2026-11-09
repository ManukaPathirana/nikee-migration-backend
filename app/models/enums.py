from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    STAFF = "staff"
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
