import uuid
from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel, Field, HttpUrl, model_validator

from app.models.enums import MeetingMode, MeetingStatus
from app.schemas.common import ORMModel
from app.schemas.inquiry import UserBrief

MAX_SLOT_LENGTH = timedelta(hours=4)


class SlotIn(BaseModel):
    start_at: AwareDatetime
    end_at: AwareDatetime

    @model_validator(mode="after")
    def _valid_range(self) -> "SlotIn":
        if self.end_at <= self.start_at:
            raise ValueError("A time slot must end after it starts")
        if self.end_at - self.start_at > MAX_SLOT_LENGTH:
            raise ValueError("A time slot can be at most 4 hours long")
        return self


class ProposeIn(BaseModel):
    slots: list[SlotIn] = Field(min_length=1, max_length=3)
    staff_note: str | None = Field(default=None, max_length=1000)


class MeetingCreate(ProposeIn):
    inquiry_id: uuid.UUID
    mode: MeetingMode
    location: str | None = Field(default=None, max_length=300)
    meeting_link: HttpUrl | None = None

    @model_validator(mode="after")
    def _mode_details(self) -> "MeetingCreate":
        if self.mode == MeetingMode.IN_PERSON and not (self.location or "").strip():
            raise ValueError("Add the address for an in-person meeting")
        if self.mode == MeetingMode.ONLINE and self.meeting_link is None:
            raise ValueError("Add the video call link for an online meeting")
        return self


class SelectSlotIn(BaseModel):
    slot_id: uuid.UUID


class RescheduleIn(BaseModel):
    note: str = Field(min_length=3, max_length=1000)


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class SlotOut(ORMModel):
    id: uuid.UUID
    start_at: datetime
    end_at: datetime
    is_selected: bool


class InquiryRef(ORMModel):
    id: uuid.UUID
    ref_no: str
    subject: str


class MeetingOut(ORMModel):
    id: uuid.UUID
    inquiry: InquiryRef
    staff: UserBrief
    customer: UserBrief
    status: MeetingStatus
    mode: MeetingMode
    location: str | None
    meeting_link: str | None
    confirmed_slot: SlotOut | None
    slots: list[SlotOut]
    customer_note: str | None
    staff_note: str | None
    created_at: datetime
    updated_at: datetime
