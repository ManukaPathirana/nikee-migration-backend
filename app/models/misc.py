"""Reviews, notifications, company info and audit logs."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, String, Text, false, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin

DEFAULT_WEBSITE = "https://nikeemigration.com"


class Review(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "reviews"
    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),)

    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    inquiry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inquiries.id", ondelete="CASCADE"), unique=True
    )
    staff_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    rating: Mapped[int] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    is_visible: Mapped[bool] = mapped_column(default=True, server_default=true())
    admin_reply: Mapped[str | None] = mapped_column(Text)
    flagged: Mapped[bool] = mapped_column(default=False, server_default=false())


class Notification(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_id_read_at", "user_id", "read_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    read_at: Mapped[datetime | None]


class CompanyInfo(UUIDPkMixin, TimestampMixin, Base):
    """Single-row table; services always read/update the first row."""

    __tablename__ = "company_info"

    name: Mapped[str] = mapped_column(String(200))
    about: Mapped[str | None] = mapped_column(Text)
    website_url: Mapped[str] = mapped_column(
        String(300), default=DEFAULT_WEBSITE, server_default=DEFAULT_WEBSITE
    )
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(320))
    whatsapp: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(Text)
    socials: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class AuditLog(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "audit_logs"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
