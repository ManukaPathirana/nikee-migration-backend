"""Meetings: staff propose 1-3 slots, the customer picks one (or asks for another time),
everyone gets emails (with .ics once confirmed) and reminders 24h and 1h before."""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.config import settings
from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.core.permissions import can_access_team, is_admin
from app.core.security import utcnow
from app.models import (
    Inquiry,
    InquiryStatus,
    Meeting,
    MeetingMode,
    MeetingSlot,
    MeetingStatus,
    User,
    UserRole,
)
from app.schemas.common import PageParams
from app.schemas.meeting import SlotIn
from app.services import job_queue
from app.services.audit_service import audit
from app.services.ics_service import MODE_LABEL, ics_attachment, where
from app.services.notification_service import notify

M = MeetingStatus
OPEN = {M.PROPOSED, M.CONFIRMED, M.RESCHEDULE_REQUESTED}


# ---------------------------------------------------------------- helpers


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.business_timezone)


def fmt_slot(start: datetime, end: datetime) -> str:
    s, e = start.astimezone(_tz()), end.astimezone(_tz())
    return f"{s:%a %d %b %Y, %I:%M %p} – {e:%I:%M %p}".replace(" 0", " ")


def _query() -> Select[tuple[Meeting]]:
    return (
        select(Meeting)
        .options(
            selectinload(Meeting.slots),
            joinedload(Meeting.confirmed_slot),
            joinedload(Meeting.inquiry),
            joinedload(Meeting.staff),
            joinedload(Meeting.customer),
        )
        .execution_options(populate_existing=True)
    )


def _admin_of(meeting: Meeting, user: User) -> bool:
    """Super admin, or the team admin of the meeting's inquiry."""
    return is_admin(user) and can_access_team(user, meeting.inquiry.visa_type_id)


def _can_manage(meeting: Meeting, user: User) -> bool:
    return _admin_of(meeting, user) or meeting.staff_id == user.id


def _validate_slots(slots: list[SlotIn]) -> None:
    now = utcnow()
    for s in slots:
        if s.start_at <= now + timedelta(minutes=15):
            raise BadRequest(
                "Proposed times must be at least 15 minutes in the future", code="slot_in_past"
            )
    ordered = sorted(slots, key=lambda s: s.start_at)
    for a, b in zip(ordered, ordered[1:], strict=False):
        if b.start_at < a.end_at:
            raise BadRequest("Proposed times must not overlap", code="slots_overlap")


def _email_context(meeting: Meeting, recipient: User, **extra: object) -> dict[str, object]:
    slot = meeting.confirmed_slot
    return {
        "name": recipient.full_name,
        "how": f"{MODE_LABEL[meeting.mode]} with {meeting.staff.full_name}",
        "where": where(meeting),
        "link": meeting.meeting_link,
        "when": (
            fmt_slot(slot.start_at, slot.end_at) + f" ({settings.business_timezone})"
            if slot
            else None
        ),
        "ref_no": meeting.inquiry.ref_no,
        "subject": meeting.inquiry.subject,
        "timezone": settings.business_timezone,
        **extra,
    }


# ---------------------------------------------------------------- reads


async def get_meeting(db: AsyncSession, meeting_id: uuid.UUID, user: User) -> Meeting:
    meeting = await db.scalar(_query().where(Meeting.id == meeting_id))
    if meeting is None:
        raise NotFound("Meeting not found")
    visible = (
        _admin_of(meeting, user) or meeting.staff_id == user.id or meeting.customer_id == user.id
    )
    if not visible:
        raise NotFound("Meeting not found")
    return meeting


async def list_meetings(
    db: AsyncSession,
    user: User,
    params: PageParams,
    *,
    status: MeetingStatus | None = None,
    inquiry_id: uuid.UUID | None = None,
    start_from: datetime | None = None,
    start_to: datetime | None = None,
) -> tuple[list[Meeting], int]:
    query = _query()
    if user.role == UserRole.CUSTOMER:
        query = query.where(Meeting.customer_id == user.id)
    elif user.role == UserRole.STAFF:
        query = query.where(Meeting.staff_id == user.id)
    elif user.role == UserRole.ADMIN:  # team admin: meetings of their team's inquiries
        query = query.join(Inquiry, Inquiry.id == Meeting.inquiry_id).where(
            Inquiry.visa_type_id == user.team_id
        )
    if status is not None:
        query = query.where(Meeting.status == status)
    if inquiry_id is not None:
        query = query.where(Meeting.inquiry_id == inquiry_id)
    if start_from is not None or start_to is not None:
        query = query.join(MeetingSlot, MeetingSlot.id == Meeting.confirmed_slot_id)
        if start_from is not None:
            query = query.where(MeetingSlot.start_at >= start_from)
        if start_to is not None:
            query = query.where(MeetingSlot.start_at < start_to)
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = await db.scalars(
        query.order_by(Meeting.updated_at.desc()).offset(params.offset).limit(params.page_size)
    )
    return list(rows), total or 0


# ---------------------------------------------------------------- staff actions


async def create_meeting(
    db: AsyncSession,
    staff: User,
    *,
    inquiry_id: uuid.UUID,
    mode: MeetingMode,
    location: str | None,
    meeting_link: str | None,
    slots: list[SlotIn],
    staff_note: str | None,
) -> Meeting:
    inquiry = await db.scalar(
        select(Inquiry).where(Inquiry.id == inquiry_id).options(joinedload(Inquiry.customer))
    )
    if inquiry is None or not can_access_team(staff, inquiry.visa_type_id):
        raise NotFound("Inquiry not found")
    if inquiry.status in (InquiryStatus.COMPLETED, InquiryStatus.CANCELLED):
        raise BadRequest("This inquiry is closed", code="inquiry_closed")
    if staff.role == UserRole.STAFF:
        if inquiry.assigned_staff_id is None:
            inquiry.assigned_staff_id = staff.id  # scheduling a meeting claims the inquiry
        elif inquiry.assigned_staff_id != staff.id:
            raise Forbidden("This inquiry is assigned to another team member", code="not_assigned")
    elif inquiry.assigned_staff_id is None:
        # Meetings are always hosted by a consultant; the admin assigns one first.
        raise BadRequest(
            "Assign this inquiry to a staff member before scheduling a meeting",
            code="needs_assignment",
        )
    if await db.scalar(
        select(Meeting.id)
        .where(Meeting.inquiry_id == inquiry.id, Meeting.status.in_(OPEN))
        .limit(1)
    ):
        raise Conflict(
            "This inquiry already has an open meeting. Open it to propose new times instead.",
            code="meeting_exists",
        )
    _validate_slots(slots)

    # The assigned consultant hosts the meeting (also when the admin schedules it).
    host_id = inquiry.assigned_staff_id

    meeting = Meeting(
        inquiry_id=inquiry.id,
        staff_id=host_id,
        customer_id=inquiry.customer_id,
        status=M.PROPOSED,
        mode=mode,
        location=(location or "").strip() or None,
        meeting_link=meeting_link,
        staff_note=(staff_note or "").strip() or None,
    )
    meeting.slots = [MeetingSlot(start_at=s.start_at, end_at=s.end_at) for s in slots]
    db.add(meeting)
    await db.flush()
    audit(db, actor_id=staff.id, action="meeting.propose", entity="meeting", entity_id=meeting.id)
    await db.commit()
    meeting = await get_meeting(db, meeting.id, staff)
    await _announce_proposal(db, meeting)
    return meeting


async def propose_again(
    db: AsyncSession,
    meeting: Meeting,
    staff: User,
    *,
    slots: list[SlotIn],
    staff_note: str | None,
) -> None:
    if not _can_manage(meeting, staff):
        raise Forbidden("Only the meeting host can propose new times", code="not_host")
    if meeting.status not in (M.PROPOSED, M.RESCHEDULE_REQUESTED):
        raise BadRequest(
            "New times can only be proposed before a time is confirmed", code="invalid_status"
        )
    _validate_slots(slots)
    meeting.slots = [MeetingSlot(start_at=s.start_at, end_at=s.end_at) for s in slots]
    meeting.status = M.PROPOSED
    if staff_note is not None:
        meeting.staff_note = staff_note.strip() or None
    audit(db, actor_id=staff.id, action="meeting.propose", entity="meeting", entity_id=meeting.id)
    await db.commit()
    await _announce_proposal(db, await get_meeting(db, meeting.id, staff))


async def _announce_proposal(db: AsyncSession, meeting: Meeting) -> None:
    notify(
        db,
        user_id=meeting.customer_id,
        type="meeting_proposed",
        title="Choose a meeting time",
        body=f"{meeting.staff.full_name} proposed {len(meeting.slots)} time(s) for "
        f"{meeting.inquiry.ref_no}.",
        data={"meeting_id": str(meeting.id)},
    )
    await db.commit()
    await job_queue.send_email(
        to=meeting.customer.email,
        subject=f"Choose a time to meet — {meeting.inquiry.ref_no}",
        template="meeting_update.html",
        context=_email_context(
            meeting,
            meeting.customer,
            heading="Let's find a time to meet",
            message=f"{meeting.staff.full_name} would like to meet about your inquiry. "
            "Open the app to pick the time that suits you, or ask for another time.",
            slots=[fmt_slot(s.start_at, s.end_at) for s in meeting.slots],
            note=meeting.staff_note,
            action_hint="Pick a time in the Meetings tab of the Nikee Migrations app.",
        ),
    )


async def complete(db: AsyncSession, meeting: Meeting, user: User) -> None:
    if not _can_manage(meeting, user):
        raise Forbidden("Only the meeting host or an admin can do this", code="not_host")
    if meeting.status != M.CONFIRMED:
        raise BadRequest("Only confirmed meetings can be marked completed", code="invalid_status")
    meeting.status = M.COMPLETED
    audit(db, actor_id=user.id, action="meeting.complete", entity="meeting", entity_id=meeting.id)
    notify(
        db,
        user_id=meeting.customer_id,
        type="meeting_completed",
        title="Thanks for meeting with us",
        body=f"Your meeting about {meeting.inquiry.ref_no} is marked as completed.",
        data={"meeting_id": str(meeting.id)},
    )
    await db.commit()
    await job_queue.send_email(
        to=meeting.customer.email,
        subject=f"Thanks for meeting with us — {meeting.inquiry.ref_no}",
        template="meeting_update.html",
        context=_email_context(
            meeting,
            meeting.customer,
            heading="Thanks for meeting with us",
            message="Your meeting is marked as completed. Your consultant will follow up "
            "on the next steps in your inquiry.",
        ),
    )


async def cancel(db: AsyncSession, meeting: Meeting, user: User, reason: str) -> None:
    if not _can_manage(meeting, user):
        raise Forbidden("Only the meeting host or an admin can cancel", code="not_host")
    if meeting.status not in OPEN:
        raise BadRequest("This meeting is already closed", code="invalid_status")
    was_confirmed = meeting.status == M.CONFIRMED
    meeting.status = M.CANCELLED
    meeting.staff_note = reason.strip()
    if was_confirmed:
        meeting.ics_sequence += 1
    audit(
        db,
        actor_id=user.id,
        action="meeting.cancel",
        entity="meeting",
        entity_id=meeting.id,
        meta={"reason": reason},
    )
    notify(
        db,
        user_id=meeting.customer_id,
        type="meeting_cancelled",
        title="Meeting cancelled",
        body=reason,
        data={"meeting_id": str(meeting.id)},
    )
    await db.commit()
    attachments = [ics_attachment(meeting, cancel=True)] if was_confirmed else None
    for person in (meeting.customer, meeting.staff):
        await job_queue.send_email(
            to=person.email,
            subject=f"Meeting cancelled — {meeting.inquiry.ref_no}",
            template="meeting_update.html",
            context=_email_context(
                meeting,
                person,
                heading="Meeting cancelled",
                message="This meeting has been cancelled.",
                note=f"Reason: {reason}",
            ),
            attachments=attachments,
        )


# ---------------------------------------------------------------- customer actions


def _require_customer(meeting: Meeting, user: User) -> None:
    if meeting.customer_id != user.id:
        raise Forbidden("Only the customer can respond to this meeting", code="not_customer")


async def select_slot(db: AsyncSession, meeting: Meeting, user: User, slot_id: uuid.UUID) -> None:
    _require_customer(meeting, user)
    if meeting.status != M.PROPOSED:
        raise BadRequest("This meeting isn't waiting for a time choice", code="invalid_status")
    slot = next((s for s in meeting.slots if s.id == slot_id), None)
    if slot is None:
        raise BadRequest("Choose one of the proposed times", code="invalid_slot")
    now = utcnow()
    if slot.start_at <= now:
        raise BadRequest(
            "That time has already passed. Please ask for another time.", code="slot_in_past"
        )
    for s in meeting.slots:
        s.is_selected = s.id == slot.id
    meeting.confirmed_slot = slot
    meeting.confirmed_slot_id = slot.id
    meeting.status = M.CONFIRMED
    # Don't send reminders whose moment has already passed.
    if slot.start_at - now <= timedelta(hours=24):
        meeting.reminder_24h_sent_at = now
    if slot.start_at - now <= timedelta(hours=1):
        meeting.reminder_1h_sent_at = now
    notify(
        db,
        user_id=meeting.staff_id,
        type="meeting_confirmed",
        title=f"Meeting confirmed — {meeting.inquiry.ref_no}",
        body=f"{meeting.customer.full_name} chose {fmt_slot(slot.start_at, slot.end_at)}.",
        data={"meeting_id": str(meeting.id)},
    )
    await db.commit()
    meeting = await get_meeting(db, meeting.id, user)
    invite = ics_attachment(meeting)
    for person in (meeting.customer, meeting.staff):
        await job_queue.send_email(
            to=person.email,
            subject=f"Meeting confirmed — {meeting.inquiry.ref_no}",
            template="meeting_update.html",
            context=_email_context(
                meeting,
                person,
                heading="Your meeting is confirmed",
                message="The calendar invite is attached. We'll also remind you a day before "
                "and an hour before.",
                note=meeting.staff_note if person.id == meeting.customer_id else None,
            ),
            attachments=[invite],
        )


async def request_reschedule(db: AsyncSession, meeting: Meeting, user: User, note: str) -> None:
    _require_customer(meeting, user)
    if meeting.status != M.PROPOSED:
        raise BadRequest(
            "You can only ask for another time before confirming", code="invalid_status"
        )
    meeting.status = M.RESCHEDULE_REQUESTED
    meeting.customer_note = note.strip()
    notify(
        db,
        user_id=meeting.staff_id,
        type="meeting_reschedule_requested",
        title=f"New time requested — {meeting.inquiry.ref_no}",
        body=meeting.customer_note,
        data={"meeting_id": str(meeting.id)},
    )
    await db.commit()
    await job_queue.send_email(
        to=meeting.staff.email,
        subject=f"{meeting.customer.full_name} asked for another time — {meeting.inquiry.ref_no}",
        template="meeting_update.html",
        context=_email_context(
            meeting,
            meeting.staff,
            heading="The customer asked for another time",
            message="None of the proposed times work. Please propose new times in the app.",
            note=f"Customer's note: {meeting.customer_note}",
        ),
    )


# ---------------------------------------------------------------- reminders


async def send_due_reminders(db: AsyncSession, now: datetime | None = None) -> int:
    """Send 24h and 1h reminders that are due. Safe to run often and from several workers:
    each reminder is claimed with a conditional UPDATE so it is sent once."""
    now = now or utcnow()
    sent = 0
    windows = (
        ("reminder_24h_sent_at", timedelta(hours=24), "tomorrow", "in about 24 hours"),
        ("reminder_1h_sent_at", timedelta(hours=1), "soon", "in about an hour"),
    )
    for column_name, ahead, _, phrase in windows:
        column = getattr(Meeting, column_name)
        due = await db.scalars(
            select(Meeting.id)
            .join(MeetingSlot, MeetingSlot.id == Meeting.confirmed_slot_id)
            .where(
                Meeting.status == M.CONFIRMED,
                column.is_(None),
                MeetingSlot.start_at > now,
                MeetingSlot.start_at <= now + ahead,
            )
        )
        for meeting_id in list(due):
            claimed = await db.scalar(
                update(Meeting)
                .where(Meeting.id == meeting_id, column.is_(None))
                .values({column_name: now})
                .returning(Meeting.id)
            )
            await db.commit()
            if claimed is None:
                continue  # another worker took it
            meeting = await db.scalar(_query().where(Meeting.id == meeting_id))
            if meeting is None:
                continue
            for person in (meeting.customer, meeting.staff):
                await job_queue.send_email(
                    to=person.email,
                    subject=f"Reminder: meeting {phrase} — {meeting.inquiry.ref_no}",
                    template="meeting_update.html",
                    context=_email_context(
                        meeting,
                        person,
                        heading=f"Your meeting starts {phrase}",
                        message="Here are the details.",
                    ),
                )
            notify(
                db,
                user_id=meeting.customer_id,
                type="meeting_reminder",
                title=f"Meeting {phrase}",
                body=fmt_slot(meeting.confirmed_slot.start_at, meeting.confirmed_slot.end_at),
                data={"meeting_id": str(meeting.id)},
            )
            await db.commit()
            sent += 1
    return sent
