import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin, pg_enum
from app.models.enums import MeetingMode, MeetingStatus


class Meeting(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "meetings"
    __table_args__ = (Index("ix_meetings_staff_id_status", "staff_id", "status"),)

    inquiry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inquiries.id", ondelete="CASCADE"), index=True
    )
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[MeetingStatus] = mapped_column(
        pg_enum(MeetingStatus, "meeting_status"),
        default=MeetingStatus.PROPOSED,
        server_default=MeetingStatus.PROPOSED.value,
    )
    mode: Mapped[MeetingMode] = mapped_column(pg_enum(MeetingMode, "meeting_mode"))
    location: Mapped[str | None] = mapped_column(String(300))
    meeting_link: Mapped[str | None] = mapped_column(String(500))
    # use_alter: meetings <-> meeting_slots reference each other.
    confirmed_slot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "meeting_slots.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_meetings_confirmed_slot_id_meeting_slots",
        )
    )
    customer_note: Mapped[str | None] = mapped_column(Text)
    staff_note: Mapped[str | None] = mapped_column(Text)

    slots: Mapped[list["MeetingSlot"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        foreign_keys="MeetingSlot.meeting_id",
        order_by="MeetingSlot.start_at",
    )


class MeetingSlot(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "meeting_slots"

    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    start_at: Mapped[datetime] = mapped_column(index=True)
    end_at: Mapped[datetime]
    is_selected: Mapped[bool] = mapped_column(default=False, server_default=false())

    meeting: Mapped[Meeting] = relationship(back_populates="slots", foreign_keys=[meeting_id])
