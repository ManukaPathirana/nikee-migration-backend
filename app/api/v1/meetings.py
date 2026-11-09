import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import CurrentUser, CustomerUser, DbSession, StaffOrAdmin
from app.models import MeetingStatus
from app.schemas.common import Page, PageParams, page_params
from app.schemas.meeting import (
    CancelIn,
    MeetingCreate,
    MeetingOut,
    ProposeIn,
    RescheduleIn,
    SelectSlotIn,
)
from app.services import meeting_service as svc

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.get("", response_model=Page[MeetingOut])
async def list_meetings(
    user: CurrentUser,
    db: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[MeetingStatus | None, Query(alias="status")] = None,
    inquiry_id: uuid.UUID | None = None,
    start_from: datetime | None = None,
    start_to: datetime | None = None,
) -> Page[MeetingOut]:
    """Customers see their meetings, staff the meetings they host, admins all.
    `start_from`/`start_to` filter confirmed meetings by start time (calendar views)."""
    rows, total = await svc.list_meetings(
        db,
        user,
        params,
        status=status_filter,
        inquiry_id=inquiry_id,
        start_from=start_from,
        start_to=start_to,
    )
    return Page(
        items=[MeetingOut.model_validate(m) for m in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.post("", response_model=MeetingOut, status_code=status.HTTP_201_CREATED)
async def create_meeting(data: MeetingCreate, user: StaffOrAdmin, db: DbSession) -> MeetingOut:
    """Propose 1-3 time slots for an inquiry. The customer is emailed and notified."""
    meeting = await svc.create_meeting(
        db,
        user,
        inquiry_id=data.inquiry_id,
        mode=data.mode,
        location=data.location,
        meeting_link=str(data.meeting_link) if data.meeting_link else None,
        slots=data.slots,
        staff_note=data.staff_note,
    )
    return MeetingOut.model_validate(meeting)


@router.get("/{meeting_id}", response_model=MeetingOut)
async def get_meeting(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession) -> MeetingOut:
    return MeetingOut.model_validate(await svc.get_meeting(db, meeting_id, user))


@router.post("/{meeting_id}/propose", response_model=MeetingOut)
async def propose_again(
    meeting_id: uuid.UUID, data: ProposeIn, user: StaffOrAdmin, db: DbSession
) -> MeetingOut:
    """Replace the proposed slots (e.g. after the customer asked for another time)."""
    meeting = await svc.get_meeting(db, meeting_id, user)
    await svc.propose_again(db, meeting, user, slots=data.slots, staff_note=data.staff_note)
    return MeetingOut.model_validate(await svc.get_meeting(db, meeting_id, user))


@router.post("/{meeting_id}/select-slot", response_model=MeetingOut)
async def select_slot(
    meeting_id: uuid.UUID, data: SelectSlotIn, user: CustomerUser, db: DbSession
) -> MeetingOut:
    """Customer confirms a time. Both sides get an email with an .ics invite."""
    meeting = await svc.get_meeting(db, meeting_id, user)
    await svc.select_slot(db, meeting, user, data.slot_id)
    return MeetingOut.model_validate(await svc.get_meeting(db, meeting_id, user))


@router.post("/{meeting_id}/request-reschedule", response_model=MeetingOut)
async def request_reschedule(
    meeting_id: uuid.UUID, data: RescheduleIn, user: CustomerUser, db: DbSession
) -> MeetingOut:
    meeting = await svc.get_meeting(db, meeting_id, user)
    await svc.request_reschedule(db, meeting, user, data.note)
    return MeetingOut.model_validate(await svc.get_meeting(db, meeting_id, user))


@router.post("/{meeting_id}/cancel", response_model=MeetingOut)
async def cancel_meeting(
    meeting_id: uuid.UUID, data: CancelIn, user: StaffOrAdmin, db: DbSession
) -> MeetingOut:
    meeting = await svc.get_meeting(db, meeting_id, user)
    await svc.cancel(db, meeting, user, data.reason)
    return MeetingOut.model_validate(await svc.get_meeting(db, meeting_id, user))


@router.post("/{meeting_id}/complete", response_model=MeetingOut)
async def complete_meeting(meeting_id: uuid.UUID, user: StaffOrAdmin, db: DbSession) -> MeetingOut:
    meeting = await svc.get_meeting(db, meeting_id, user)
    await svc.complete(db, meeting, user)
    return MeetingOut.model_validate(await svc.get_meeting(db, meeting_id, user))
