"""Import every model here so Alembic and relationships see the full metadata."""

from app.db.base import Base
from app.models.attendance import ActivityEvent, WorkSession
from app.models.enums import (
    ApplicationStatus,
    DocumentStatus,
    InquiryStatus,
    MeetingMode,
    MeetingStatus,
    UserRole,
    VerificationPurpose,
)
from app.models.inquiry import Inquiry, InquiryNote, InquiryStatusHistory
from app.models.meeting import Meeting, MeetingSlot
from app.models.misc import AuditLog, CompanyInfo, Notification, Review
from app.models.user import (
    CustomerProfile,
    DeviceToken,
    EmailVerification,
    RefreshToken,
    StaffProfile,
    User,
)
from app.models.visa import (
    ApplicationDocument,
    DocumentRequirement,
    VisaApplication,
    VisaType,
)

__all__ = [
    "ActivityEvent",
    "ApplicationDocument",
    "ApplicationStatus",
    "AuditLog",
    "Base",
    "CompanyInfo",
    "CustomerProfile",
    "DeviceToken",
    "DocumentRequirement",
    "DocumentStatus",
    "EmailVerification",
    "Inquiry",
    "InquiryNote",
    "InquiryStatus",
    "InquiryStatusHistory",
    "Meeting",
    "MeetingMode",
    "MeetingSlot",
    "MeetingStatus",
    "Notification",
    "RefreshToken",
    "Review",
    "StaffProfile",
    "User",
    "UserRole",
    "VerificationPurpose",
    "VisaApplication",
    "VisaType",
    "WorkSession",
]
