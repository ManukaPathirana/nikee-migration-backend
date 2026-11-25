import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.security import utcnow
from app.db.base import Base, TimestampMixin, UUIDPkMixin, pg_enum
from app.models.enums import InquiryStatus

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.visa import VisaType


class Inquiry(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "inquiries"
    __table_args__ = (
        Index("ix_inquiries_status_assigned_staff_id", "status", "assigned_staff_id"),
    )

    ref_no: Mapped[str] = mapped_column(String(20), unique=True)  # e.g. NM-2026-0001
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    # The visa type the inquiry is about = the team that handles it.
    visa_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("visa_types.id"), index=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("visa_applications.id", ondelete="SET NULL")
    )
    # consultation: a meeting with the migration agent (super admin), booked by the assigned
    # staff member in the agent's calendar. general: handled by the team admin, who books meetings
    # in their own team calendar.
    kind: Mapped[str] = mapped_column(
        String(20), default="consultation", server_default="consultation"
    )
    subject: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(Text)
    preferred_contact_time: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[InquiryStatus] = mapped_column(
        pg_enum(InquiryStatus, "inquiry_status"),
        default=InquiryStatus.SCHEDULED,
        server_default=InquiryStatus.SCHEDULED.value,
    )
    assigned_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    closed_at: Mapped[datetime | None]
    # Consultation fee, paid outside the app (bank transfer). The team admin sets the amount, the
    # customer uploads a receipt, the team admin approves it; then the meeting can be booked.
    # fee_status: none | requested | receipt_uploaded | approved | rejected
    fee_status: Mapped[str] = mapped_column(String(20), default="none", server_default="none")
    fee_cents: Mapped[int | None]
    fee_currency: Mapped[str] = mapped_column(String(3), default="AUD", server_default="AUD")
    fee_receipt_key: Mapped[str | None] = mapped_column(String(500))
    fee_receipt_name: Mapped[str | None] = mapped_column(String(255))
    fee_receipt_mime: Mapped[str | None] = mapped_column(String(100))
    fee_receipt_uploaded_at: Mapped[datetime | None]
    fee_rejection_reason: Mapped[str | None] = mapped_column(Text)
    fee_approved_at: Mapped[datetime | None]

    customer: Mapped["User"] = relationship(foreign_keys=[customer_id])
    assigned_staff: Mapped["User | None"] = relationship(foreign_keys=[assigned_staff_id])
    visa_type: Mapped["VisaType"] = relationship(lazy="joined")
    history: Mapped[list["InquiryStatusHistory"]] = relationship(
        cascade="all, delete-orphan", order_by="InquiryStatusHistory.changed_at"
    )
    notes: Mapped[list["InquiryNote"]] = relationship(
        cascade="all, delete-orphan", order_by="InquiryNote.created_at"
    )


class InquiryStatusHistory(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "inquiry_status_history"

    inquiry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inquiries.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[InquiryStatus | None] = mapped_column(
        pg_enum(InquiryStatus, "inquiry_status")
    )
    to_status: Mapped[InquiryStatus] = mapped_column(pg_enum(InquiryStatus, "inquiry_status"))
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(default=utcnow)

    actor: Mapped["User | None"] = relationship(foreign_keys=[changed_by])


class InquiryNote(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "inquiry_notes"

    inquiry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inquiries.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    is_internal: Mapped[bool] = mapped_column(default=True, server_default=true())

    author: Mapped["User | None"] = relationship(foreign_keys=[author_id])
