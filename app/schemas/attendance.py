import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import UserRole
from app.schemas.common import ORMModel
from app.schemas.inquiry import TeamBrief


class WorkSessionOut(ORMModel):
    id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    end_reason: str | None


class WorkStatusOut(BaseModel):
    """The signed-in person's clock."""

    working: bool
    current: WorkSessionOut | None
    today_seconds: int
    week_seconds: int


class ActivityEventOut(ORMModel):
    id: uuid.UUID
    kind: str  # login | logout
    device_info: str | None
    created_at: datetime


class StaffReportRow(BaseModel):
    """How one team member worked in the chosen period."""

    user_id: uuid.UUID
    full_name: str
    email: str
    role: UserRole
    team: TeamBrief | None
    is_active: bool
    working_now: bool
    current_started_at: datetime | None
    worked_seconds: int
    shifts: int
    first_login_at: datetime | None
    last_login_at: datetime | None
    last_logout_at: datetime | None
    open_inquiries: int
    inquiries_completed: int
    status_changes: int
    documents_reviewed: int
    meetings_completed: int
    actions: int  # all recorded actions (audit log)


class StaffReportOut(BaseModel):
    period_start: datetime
    period_end: datetime
    rows: list[StaffReportRow]


class AttendanceDetailOut(BaseModel):
    user: StaffReportRow
    sessions: list[WorkSessionOut]
    events: list[ActivityEventOut]
