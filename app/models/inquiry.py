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


class Inquiry(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "inquiries"
    __table_args__ = (
        Index("ix_inquiries_status_assigned_staff_id", "status", "assigned_staff_id"),
    )

    ref_no: Mapped[str] = mapped_column(String(20), unique=True)  # e.g. NM-2026-0001
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("visa_applications.id", ondelete="SET NULL")
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

    customer: Mapped["User"] = relationship(foreign_keys=[customer_id])
    assigned_staff: Mapped["User | None"] = relationship(foreign_keys=[assigned_staff_id])
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
