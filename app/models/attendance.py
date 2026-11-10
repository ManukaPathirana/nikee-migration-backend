"""Work hours: Start/End work shifts and sign-in/sign-out events for staff and admins."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import utcnow
from app.db.base import Base, TimestampMixin, UUIDPkMixin


class WorkSession(UUIDPkMixin, TimestampMixin, Base):
    """One shift: from 'Start work' until 'End work' (or sign-out)."""

    __tablename__ = "work_sessions"
    __table_args__ = (Index("ix_work_sessions_user_id_started_at", "user_id", "started_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    ended_at: Mapped[datetime | None]
    # 'manual' (End work button) or 'logout' (ended automatically at sign-out)
    end_reason: Mapped[str | None] = mapped_column(String(20))


class ActivityEvent(UUIDPkMixin, TimestampMixin, Base):
    """Sign-in / sign-out of staff and admins (customers are not tracked)."""

    __tablename__ = "activity_events"
    __table_args__ = (Index("ix_activity_events_user_id_created_at", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # 'login' | 'logout'
    device_info: Mapped[str | None] = mapped_column(String(255))
