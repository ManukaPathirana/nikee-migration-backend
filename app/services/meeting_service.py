"""Meetings, booked in one shared calendar.

The super admin attends every meeting, so all teams share one calendar of 30-minute slots
(weekdays 9:00-17:00, business time zone) and a slot can hold only one meeting. Staff (or the
team admin) book a free slot: the meeting is confirmed at once and the customer and super admin
are told the date, time and link. The super admin can decline a time; staff then book another.
Everyone gets emails (with .ics) and reminders 24h and 1h before.
Older meetings may still be "proposed" (the customer picked from 1-3 times); that still works.
"""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.config import settings
from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.core.permissions import can_access_team, is_admin
from app.core.security import utcnow
from app.core.viewer import contact_name
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
from app.services.notification_service import notify, notify_super_admins

M = MeetingStatus
OPEN = {M.PROPOSED, M.CONFIRMED, M.RESCHEDULE_REQUESTED}

SLOT_MINUTES = 30
DAY_START = time(9, 0)
DAY_END = time(17, 0)
WORKDAYS = {0, 1, 2, 3, 4}  # Monday-Friday
MEETING_MINUTES = 60  # a meeting takes two back-to-back 30-minute slots
_BOOKING_LOCK = 731_001  # pg advisory lock: one booking at a time per calendar


# ---------------------------------------------------------------- helpers


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.business_timezone)


def business_today() -> date:
    return utcnow().astimezone(_tz()).date()


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


def calendar_team(inquiry: Inquiry) -> uuid.UUID | None:
    """Which calendar a meeting goes in: None = the migration agent's shared calendar
    (consultations), else the team admin's calendar of that team (general inquiries)."""
    return inquiry.visa_type_id if inquiry.kind == "general" else None


def _validate_booking(start: datetime, end: datetime) -> None:
    """Two back-to-back 30-minute slots (1 hour) starting on the hour or half hour, weekdays
    9-5 in the business time zone."""
    local = start.astimezone(_tz())
    if end - start != timedelta(minutes=MEETING_MINUTES):
        raise BadRequest(
            "Meetings are 1 hour long (two 30-minute slots)", code="invalid_slot_length"
        )
    if local.minute not in (0, 30) or local.second or local.microsecond:
        raise BadRequest("Pick a time on the hour or half hour", code="invalid_slot")
    if local.weekday() not in WORKDAYS:
        raise BadRequest("Meetings can be booked Monday to Friday", code="outside_hours")
    if local.time() < DAY_START or (local + timedelta(minutes=MEETING_MINUTES)).time() > DAY_END:
        raise BadRequest("Meetings can be booked between 9 AM and 5 PM", code="outside_hours")
    if start <= utcnow() + timedelta(minutes=15):
        raise BadRequest("Pick a time at least 15 minutes from now", code="slot_in_past")


def _in_calendar(query: Select, team_id: uuid.UUID | None) -> Select:
    """Limit a meetings query (already joined to Inquiry) to one calendar."""
    if team_id is None:
        return query.where(Inquiry.kind == "consultation")
    return query.where(Inquiry.kind == "general", Inquiry.visa_type_id == team_id)


async def _ensure_free(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    *,
    except_id: uuid.UUID | None,
    team_id: uuid.UUID | None = None,
) -> None:
    """Locks booking in this calendar for this transaction and checks nothing overlaps."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:k, hashtext(:t))"),
        {"k": _BOOKING_LOCK, "t": str(team_id or "agent")},
    )
    query = _in_calendar(
        select(Meeting.id)
        .join(MeetingSlot, MeetingSlot.id == Meeting.confirmed_slot_id)
        .join(Inquiry, Inquiry.id == Meeting.inquiry_id)
        .where(
            Meeting.status == M.CONFIRMED,
            MeetingSlot.start_at < end,
            MeetingSlot.end_at > start,
        ),
        team_id,
    )
    if except_id is not None:
        query = query.where(Meeting.id != except_id)
    if await db.scalar(query.limit(1)):
        raise Conflict(
            "That time is already booked. Please pick another free slot.", code="slot_taken"
        )


async def _migration_agent(db: AsyncSession) -> User | None:
    """The migration agent who hosts consultations: the (first) active super admin."""
    return await db.scalar(
        select(User)
        .where(User.role == UserRole.SUPER_ADMIN, User.is_active.is_(True))
        .order_by(User.created_at)
        .limit(1)
    )


async def _team_admins(db: AsyncSession, team_id: uuid.UUID) -> list[User]:
    return list(
        await db.scalars(
            select(User).where(
                User.role == UserRole.ADMIN,
                User.team_id == team_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
    )


async def _super_admins(db: AsyncSession) -> list[User]:
    return list(
        await db.scalars(
            select(User).where(User.role == UserRole.SUPER_ADMIN, User.is_active.is_(True))
        )
    )


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
        "how": f"{MODE_LABEL[meeting.mode]} with "
        + (
            contact_name(meeting.inquiry.visa_type)
            if recipient.id == meeting.customer_id
            else meeting.staff.full_name
        ),
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
    hosted_by_me: bool = False,
) -> tuple[list[Meeting], int]:
    query = _query()
    if hosted_by_me:
        query = query.where(Meeting.staff_id == user.id)
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
    # The team admin books both kinds (the super admin may book consultations too).
    allowed = staff.role == UserRole.ADMIN or (
        staff.role == UserRole.SUPER_ADMIN and inquiry.kind == "consultation"
    )
    if not allowed:
        raise Forbidden("The team admin books meetings for inquiries", code="team_admin_only")
    agent = None
    if inquiry.kind == "consultation" and inquiry.fee_status != "approved":
        raise BadRequest(
            "Approve the consultation fee payment before booking the meeting",
            code="fee_not_approved",
        )
    if inquiry.kind == "consultation":
        agent = staff if staff.role == UserRole.SUPER_ADMIN else await _migration_agent(db)
        if agent is None:
            raise BadRequest("There is no migration agent account yet", code="no_agent")
    if await db.scalar(
        select(Meeting.id)
        .where(Meeting.inquiry_id == inquiry.id, Meeting.status.in_(OPEN))
        .limit(1)
    ):
        raise Conflict(
            "This inquiry already has an open meeting. Open it to propose new times instead.",
            code="meeting_exists",
        )
    if len(slots) != 1:
        raise BadRequest("Book one time from the calendar", code="one_slot")
    slot = slots[0]
    _validate_booking(slot.start_at, slot.end_at)
    await _ensure_free(
        db, slot.start_at, slot.end_at, except_id=None, team_id=calendar_team(inquiry)
    )

    # Consultations: the migration agent (super admin) hosts. General: the team admin.
    host_id = agent.id if agent is not None else staff.id

    meeting = Meeting(
        inquiry_id=inquiry.id,
        staff_id=host_id,
        customer_id=inquiry.customer_id,
        status=M.CONFIRMED,
        mode=mode,
        location=(location or "").strip() or None,
        meeting_link=meeting_link,
        staff_note=(staff_note or "").strip() or None,
    )
    booked = MeetingSlot(start_at=slot.start_at, end_at=slot.end_at, is_selected=True)
    meeting.slots = [booked]
    db.add(meeting)
    await db.flush()
    meeting.confirmed_slot = booked
    meeting.confirmed_slot_id = booked.id
    _skip_late_reminders(meeting, booked)
    audit(db, actor_id=staff.id, action="meeting.book", entity="meeting", entity_id=meeting.id)
    await db.commit()
    meeting = await get_meeting(db, meeting.id, staff)
    await _announce_booking(db, meeting, by=staff, rebooked=False)
    return meeting


async def propose_again(
    db: AsyncSession,
    meeting: Meeting,
    staff: User,
    *,
    slots: list[SlotIn],
    staff_note: str | None,
) -> None:
    """Book a new time for an open meeting (after a decline or a customer request)."""
    if not _can_manage(meeting, staff):
        raise Forbidden("Only the meeting host can book a new time", code="not_host")
    if meeting.status not in OPEN:
        raise BadRequest("This meeting is closed", code="invalid_status")
    if len(slots) != 1:
        raise BadRequest("Book one time from the calendar", code="one_slot")
    slot = slots[0]
    _validate_booking(slot.start_at, slot.end_at)
    await _ensure_free(
        db,
        slot.start_at,
        slot.end_at,
        except_id=meeting.id,
        team_id=calendar_team(meeting.inquiry),
    )
    was_booked = meeting.confirmed_slot_id is not None
    booked = MeetingSlot(start_at=slot.start_at, end_at=slot.end_at, is_selected=True)
    meeting.confirmed_slot = None
    meeting.confirmed_slot_id = None
    await db.flush()
    meeting.slots = [booked]
    await db.flush()
    meeting.confirmed_slot = booked
    meeting.confirmed_slot_id = booked.id
    meeting.status = M.CONFIRMED
    meeting.admin_note = None
    meeting.customer_note = None
    if was_booked:
        meeting.ics_sequence += 1  # the .ics update replaces the earlier invite
    meeting.reminder_24h_sent_at = None
    meeting.reminder_1h_sent_at = None
    _skip_late_reminders(meeting, booked)
    if staff_note is not None:
        meeting.staff_note = staff_note.strip() or None
    audit(db, actor_id=staff.id, action="meeting.rebook", entity="meeting", entity_id=meeting.id)
    await db.commit()
    await _announce_booking(db, await get_meeting(db, meeting.id, staff), by=staff, rebooked=True)


def _skip_late_reminders(meeting: Meeting, slot: MeetingSlot) -> None:
    """Don't send reminders whose moment has already passed."""
    now = utcnow()
    if slot.start_at - now <= timedelta(hours=24):
        meeting.reminder_24h_sent_at = now
    if slot.start_at - now <= timedelta(hours=1):
        meeting.reminder_1h_sent_at = now


def _details(meeting: Meeting) -> str:
    """Date, time and how to join, for notifications."""
    slot = meeting.confirmed_slot
    when = fmt_slot(slot.start_at, slot.end_at) if slot else ""
    how = {
        MeetingMode.ONLINE: f"Link: {meeting.meeting_link}" if meeting.meeting_link else "Online",
        MeetingMode.IN_PERSON: f"At {meeting.location}" if meeting.location else "In person",
        MeetingMode.PHONE: "Phone call",
    }[meeting.mode]
    return f"{when} ({settings.business_timezone}). {how}"


async def _announce_booking(
    db: AsyncSession, meeting: Meeting, *, by: User, rebooked: bool
) -> None:
    """Customer, super admin(s) (and the host, if someone else booked) get the date, time and
    link in a notification, and an email with the calendar invite."""
    team = meeting.inquiry.visa_type.name if meeting.inquiry.visa_type else ""
    details = _details(meeting)
    data = {"meeting_id": str(meeting.id)}
    notify(
        db,
        user_id=meeting.customer_id,
        type="meeting_confirmed",
        title="Your meeting time has changed" if rebooked else "Your meeting is booked",
        body=f"{meeting.inquiry.ref_no} with {contact_name(meeting.inquiry.visa_type)}: {details}",
        data=data,
    )
    admins = await _super_admins(db) if meeting.inquiry.kind == "consultation" else []
    for admin in admins:
        if admin.id == by.id or admin.id == meeting.staff_id:
            continue  # the host gets its own notification below
        notify(
            db,
            user_id=admin.id,
            type="meeting_booked",
            title=f"{'New time' if rebooked else 'New meeting'}: {meeting.inquiry.ref_no}",
            body=f"{team} · {meeting.staff.full_name} with {meeting.customer.full_name}. {details}",
            data=data,
        )
    if by.id != meeting.staff_id:
        notify(
            db,
            user_id=meeting.staff_id,
            type="meeting_booked",
            title=f"Meeting booked for you: {meeting.inquiry.ref_no}",
            body=details,
            data=data,
        )
    await db.commit()
    invite = ics_attachment(meeting)
    heading = "Your meeting has a new time" if rebooked else "Your meeting is booked"
    people = {p.id: p for p in [meeting.customer, meeting.staff, *admins]}
    for person in people.values():
        await job_queue.send_email(
            to=person.email,
            subject=f"{heading} — {meeting.inquiry.ref_no}",
            template="meeting_update.html",
            context=_email_context(
                meeting,
                person,
                heading=heading,
                message="The calendar invite is attached. We'll also remind you a day before "
                "and an hour before.",
                note=meeting.staff_note if person.id == meeting.customer_id else None,
            ),
            attachments=[invite],
        )


async def decline(db: AsyncSession, meeting: Meeting, user: User, reason: str) -> None:
    """Super admin: can't attend this time. The slot is freed and the host books another."""
    if user.role != UserRole.SUPER_ADMIN:
        raise Forbidden("Only the super admin can decline a meeting time", code="not_super_admin")
    if meeting.status != M.CONFIRMED:
        raise BadRequest("Only booked meetings can be declined", code="invalid_status")
    if meeting.inquiry.kind != "consultation":
        raise BadRequest(
            "This meeting is with the team admin, not in your calendar", code="not_consultation"
        )
    slot = meeting.confirmed_slot
    when = fmt_slot(slot.start_at, slot.end_at) if slot else "the booked time"
    meeting.status = M.RESCHEDULE_REQUESTED
    meeting.admin_note = reason.strip()
    audit(
        db,
        actor_id=user.id,
        action="meeting.decline",
        entity="meeting",
        entity_id=meeting.id,
        meta={"reason": reason},
    )
    data = {"meeting_id": str(meeting.id)}
    for admin in await _team_admins(db, meeting.inquiry.visa_type_id):
        notify(
            db,
            user_id=admin.id,
            type="meeting_declined",
            title=f"Book another time: {meeting.inquiry.ref_no}",
            body=f"{user.full_name} can't attend {when}. {meeting.admin_note}",
            data=data,
        )
    notify(
        db,
        user_id=meeting.customer_id,
        type="meeting_reschedule",
        title="Your meeting time will change",
        body=f"We need to move your meeting on {when}. We'll send the new time soon.",
        data=data,
    )
    await db.commit()


async def calendar(
    db: AsyncSession, user: User, start: date, days: int, team_id: uuid.UUID | None = None
) -> dict[str, object]:
    """Every 30-minute slot of the working days from `start`, with the meeting that holds it.
    team_id None: the migration agent's calendar (consultations; every team sees it).
    team_id set: that team admin's calendar (general inquiries; that team and the super admin).
    Meeting details only for meetings the viewer may open."""
    tz = _tz()
    first = datetime.combine(start, DAY_START, tz)
    last = datetime.combine(start + timedelta(days=days), DAY_END, tz)
    rows = await db.execute(
        _in_calendar(
            select(Meeting, MeetingSlot)
            .join(MeetingSlot, MeetingSlot.id == Meeting.confirmed_slot_id)
            .join(Inquiry, Inquiry.id == Meeting.inquiry_id)
            .where(
                Meeting.status.in_([M.CONFIRMED, M.COMPLETED]),
                MeetingSlot.start_at < last,
                MeetingSlot.end_at > first,
            ),
            team_id,
        ).options(
            joinedload(Meeting.inquiry).joinedload(Inquiry.visa_type),
            joinedload(Meeting.staff),
        )
    )
    booked = [(m, s) for m, s in rows.unique().all()]
    now = utcnow()
    out_days = []
    for i in range(days):
        day = start + timedelta(days=i)
        if day.weekday() not in WORKDAYS:
            continue
        slots = []
        t = datetime.combine(day, DAY_START, tz)
        end_of_day = datetime.combine(day, DAY_END, tz)
        while t + timedelta(minutes=SLOT_MINUTES) <= end_of_day:
            t_end = t + timedelta(minutes=SLOT_MINUTES)
            holder = next(((m, s) for m, s in booked if s.start_at < t_end and s.end_at > t), None)
            item: dict[str, object] = {"start_at": t, "end_at": t_end}
            if holder is not None:
                m = holder[0]
                mine = m.staff_id == user.id or _admin_of(m, user)
                item["state"] = "busy"
                item["meeting"] = {
                    "id": m.id,
                    "team": m.inquiry.visa_type.name if m.inquiry.visa_type else "",
                    "staff_name": m.staff.full_name,
                    "is_mine": mine,
                    "ref_no": m.inquiry.ref_no if mine else None,
                }
            else:
                item["state"] = "past" if t <= now + timedelta(minutes=15) else "free"
            slots.append(item)
            t = t_end
        out_days.append({"date": day.isoformat(), "slots": slots})
    return {
        "timezone": settings.business_timezone,
        "slot_minutes": SLOT_MINUTES,
        "meeting_minutes": MEETING_MINUTES,
        "team_id": team_id,
        "day_start": DAY_START.strftime("%H:%M"),
        "day_end": DAY_END.strftime("%H:%M"),
        "days": out_days,
    }


async def _announce_proposal(db: AsyncSession, meeting: Meeting) -> None:
    notify(
        db,
        user_id=meeting.customer_id,
        type="meeting_proposed",
        title="Choose a meeting time",
        body=f"{contact_name(meeting.inquiry.visa_type)} proposed {len(meeting.slots)} time(s) for "
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
            message=f"{contact_name(meeting.inquiry.visa_type)} would like to meet about your "
            "inquiry. "
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
    if was_confirmed and meeting.inquiry.kind == "consultation":
        slot = meeting.confirmed_slot
        await notify_super_admins(
            db,
            type="meeting_cancelled",
            title=f"Meeting cancelled — {meeting.inquiry.ref_no}",
            body=(f"{fmt_slot(slot.start_at, slot.end_at)} is free again. " if slot else "")
            + f"Reason: {reason}",
            data={"meeting_id": str(meeting.id)},
            skip=user.id,
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
    if meeting.status not in (M.PROPOSED, M.CONFIRMED):
        raise BadRequest("This meeting can't be changed now", code="invalid_status")
    meeting.status = M.RESCHEDULE_REQUESTED
    meeting.customer_note = note.strip()
    for admin in await _team_admins(db, meeting.inquiry.visa_type_id):
        notify(
            db,
            user_id=admin.id,
            type="meeting_reschedule_requested",
            title=f"New time requested — {meeting.inquiry.ref_no}",
            body=meeting.customer_note,
            data={"meeting_id": str(meeting.id)},
        )
    if meeting.inquiry.kind == "consultation":
        await notify_super_admins(
            db,
            type="meeting_reschedule_requested",
            title=f"Customer asked for another time — {meeting.inquiry.ref_no}",
            body=f"{meeting.customer.full_name}: {meeting.customer_note}",
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
            message="The customer needs a different time. Please book another time in the app.",
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
            admin_ids = (
                [a.id for a in await _super_admins(db)]
                if meeting.inquiry.kind == "consultation"
                else []
            )
            for person_id in {meeting.customer_id, meeting.staff_id, *admin_ids}:
                notify(
                    db,
                    user_id=person_id,
                    type="meeting_reminder",
                    title=f"Meeting {phrase}",
                    body=fmt_slot(meeting.confirmed_slot.start_at, meeting.confirmed_slot.end_at),
                    data={"meeting_id": str(meeting.id)},
                )
            await db.commit()
            sent += 1
    return sent
