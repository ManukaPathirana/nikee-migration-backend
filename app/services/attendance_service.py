"""Work hours: Start/End work shifts, sign-in/sign-out events and the staff report."""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import NotFound
from app.core.permissions import can_access_team, is_internal, is_super_admin, team_scope
from app.core.security import utcnow
from app.models import (
    ActivityEvent,
    AuditLog,
    Inquiry,
    InquiryStatus,
    InquiryStatusHistory,
    User,
    UserRole,
    WorkSession,
)
from app.schemas.attendance import (
    ActivityEventOut,
    AttendanceDetailOut,
    StaffReportOut,
    StaffReportRow,
    WorkSessionOut,
    WorkStatusOut,
)
from app.schemas.inquiry import TeamBrief

TEAM_ROLES = (UserRole.ADMIN, UserRole.STAFF)


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.business_timezone)


def day_start(now: datetime | None = None) -> datetime:
    """Midnight today in the business timezone."""
    local = (now or utcnow()).astimezone(_tz())
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(now: datetime | None = None) -> datetime:
    start = day_start(now)
    return start - timedelta(days=start.weekday())


def overlap_seconds(
    sessions: list[WorkSession], start: datetime, end: datetime, now: datetime | None = None
) -> int:
    """Seconds worked inside [start, end). Open shifts count up to now."""
    now = now or utcnow()
    total = 0.0
    for s in sessions:
        s_end = s.ended_at or now
        lo, hi = max(s.started_at, start), min(s_end, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return int(total)


async def _sessions(
    db: AsyncSession, user_ids: list[uuid.UUID], start: datetime, end: datetime
) -> list[WorkSession]:
    if not user_ids:
        return []
    rows = await db.scalars(
        select(WorkSession)
        .where(
            WorkSession.user_id.in_(user_ids),
            WorkSession.started_at < end,
            or_(WorkSession.ended_at.is_(None), WorkSession.ended_at > start),
        )
        .order_by(WorkSession.started_at)
    )
    return list(rows)


async def open_session(db: AsyncSession, user_id: uuid.UUID) -> WorkSession | None:
    return await db.scalar(
        select(WorkSession)
        .where(WorkSession.user_id == user_id, WorkSession.ended_at.is_(None))
        .order_by(WorkSession.started_at.desc())
        .limit(1)
    )


# ---------------------------------------------------------------- my clock


async def status(db: AsyncSession, user: User) -> WorkStatusOut:
    now = utcnow()
    week = week_start(now)
    sessions = await _sessions(db, [user.id], week, now + timedelta(seconds=1))
    current = next((s for s in sessions if s.ended_at is None), None)
    return WorkStatusOut(
        working=current is not None,
        current=WorkSessionOut.model_validate(current) if current else None,
        today_seconds=overlap_seconds(sessions, day_start(now), now, now),
        week_seconds=overlap_seconds(sessions, week, now, now),
    )


async def start_work(db: AsyncSession, user: User) -> WorkStatusOut:
    """Idempotent: pressing Start twice keeps the shift that is already running."""
    if await open_session(db, user.id) is None:
        db.add(WorkSession(user_id=user.id, started_at=utcnow()))
        await db.commit()
    return await status(db, user)


async def end_work(db: AsyncSession, user: User, reason: str = "manual") -> WorkStatusOut:
    session = await open_session(db, user.id)
    if session is not None:
        session.ended_at = utcnow()
        session.end_reason = reason
        await db.commit()
    return await status(db, user)


# ---------------------------------------------------------------- sign-in / sign-out


def record_event(db: AsyncSession, user: User, kind: str, device_info: str | None) -> None:
    """Log sign-in/sign-out for staff and admins (caller commits)."""
    if is_internal(user):
        db.add(
            ActivityEvent(user_id=user.id, kind=kind, device_info=(device_info or "")[:255] or None)
        )


async def on_logout(db: AsyncSession, user: User, device_info: str | None) -> None:
    """Signing out also ends a running shift."""
    if not is_internal(user):
        return
    record_event(db, user, "logout", device_info)
    session = await open_session(db, user.id)
    if session is not None:
        session.ended_at = utcnow()
        session.end_reason = "logout"
    await db.commit()


# ---------------------------------------------------------------- reports


def _period(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """Default: this week (Monday 00:00 business time) until now."""
    now = utcnow()
    return start or week_start(now), end or now


async def _team_members(
    db: AsyncSession, actor: User, team_id: uuid.UUID | None, user_id: uuid.UUID | None = None
) -> list[User]:
    query = select(User).where(User.role.in_(TEAM_ROLES))
    scope = team_scope(actor) or team_id
    if scope is not None:
        query = query.where(User.team_id == scope)
    if not is_super_admin(actor):
        # Team admins see their staff and themselves, not other admins.
        query = query.where(or_(User.role == UserRole.STAFF, User.id == actor.id))
    if user_id is not None:
        query = query.where(User.id == user_id)
    return list(await db.scalars(query.order_by(User.role, User.full_name)))


async def staff_report(
    db: AsyncSession,
    actor: User,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    team_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> StaffReportOut:
    start, end = _period(start, end)
    now = utcnow()
    users = await _team_members(db, actor, team_id, user_id)
    ids = [u.id for u in users]
    first_login, last_login, last_logout, open_inquiries = {}, {}, {}, {}
    status_changes, completed, actions, docs, meetings = {}, {}, {}, {}, {}
    by_user: dict[uuid.UUID, list[WorkSession]] = {}
    open_now: dict[uuid.UUID, datetime] = {}
    if ids:
        # Shifts in the period plus any still running, in one query.
        shifts = await db.scalars(
            select(WorkSession)
            .where(
                WorkSession.user_id.in_(ids),
                or_(
                    WorkSession.ended_at.is_(None),
                    and_(WorkSession.started_at < end, WorkSession.ended_at > start),
                ),
            )
            .order_by(WorkSession.started_at)
        )
        for sh in shifts:
            if sh.ended_at is None:
                open_now[sh.user_id] = sh.started_at
            if sh.started_at < end and (sh.ended_at is None or sh.ended_at > start):
                by_user.setdefault(sh.user_id, []).append(sh)

        login = ActivityEvent.kind == "login"
        for uid, first, last, out in await db.execute(
            select(
                ActivityEvent.user_id,
                func.min(ActivityEvent.created_at).filter(login),
                func.max(ActivityEvent.created_at).filter(login),
                func.max(ActivityEvent.created_at).filter(ActivityEvent.kind == "logout"),
            )
            .where(
                ActivityEvent.user_id.in_(ids),
                ActivityEvent.created_at >= start,
                ActivityEvent.created_at < end,
            )
            .group_by(ActivityEvent.user_id)
        ):
            first_login[uid], last_login[uid], last_logout[uid] = first, last, out

        open_inquiries = dict(
            (
                await db.execute(
                    select(Inquiry.assigned_staff_id, func.count())
                    .where(
                        Inquiry.assigned_staff_id.in_(ids),
                        Inquiry.status.in_([InquiryStatus.SCHEDULED, InquiryStatus.PROCESSING]),
                    )
                    .group_by(Inquiry.assigned_staff_id)
                )
            ).all()
        )

        for uid, changes, done in await db.execute(
            select(
                InquiryStatusHistory.changed_by,
                func.count().filter(InquiryStatusHistory.from_status.is_not(None)),
                func.count().filter(InquiryStatusHistory.to_status == InquiryStatus.COMPLETED),
            )
            .where(
                InquiryStatusHistory.changed_by.in_(ids),
                InquiryStatusHistory.changed_at >= start,
                InquiryStatusHistory.changed_at < end,
            )
            .group_by(InquiryStatusHistory.changed_by)
        ):
            status_changes[uid], completed[uid] = changes, done

        for uid, total, doc, meet in await db.execute(
            select(
                AuditLog.actor_id,
                func.count(),
                func.count().filter(AuditLog.action.like("document.%")),
                func.count().filter(AuditLog.action == "meeting.complete"),
            )
            .where(
                AuditLog.actor_id.in_(ids),
                AuditLog.created_at >= start,
                AuditLog.created_at < end,
            )
            .group_by(AuditLog.actor_id)
        ):
            actions[uid], docs[uid], meetings[uid] = total, doc, meet

    rows = [
        StaffReportRow(
            user_id=u.id,
            full_name=u.full_name,
            email=u.email,
            role=u.role,
            team=TeamBrief.model_validate(u.team) if u.team else None,
            is_active=u.is_active,
            working_now=u.id in open_now,
            current_started_at=open_now.get(u.id),
            worked_seconds=overlap_seconds(by_user.get(u.id, []), start, end, now),
            shifts=len(by_user.get(u.id, [])),
            first_login_at=first_login.get(u.id),
            last_login_at=last_login.get(u.id),
            last_logout_at=last_logout.get(u.id),
            open_inquiries=open_inquiries.get(u.id, 0),
            inquiries_completed=completed.get(u.id, 0),
            status_changes=status_changes.get(u.id, 0),
            documents_reviewed=docs.get(u.id, 0),
            meetings_completed=meetings.get(u.id, 0),
            actions=actions.get(u.id, 0),
        )
        for u in users
    ]
    return StaffReportOut(period_start=start, period_end=end, rows=rows)


async def attendance_detail(
    db: AsyncSession,
    actor: User,
    user_id: uuid.UUID,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> AttendanceDetailOut:
    target = await db.get(User, user_id)
    if (
        target is None
        or target.role not in TEAM_ROLES
        or not can_access_team(actor, target.team_id)
    ):
        raise NotFound("Team member not found")
    if not is_super_admin(actor) and target.role == UserRole.ADMIN and target.id != actor.id:
        raise NotFound("Team member not found")
    start, end = _period(start, end)
    report = await staff_report(db, actor, start=start, end=end, user_id=user_id)
    if not report.rows:
        raise NotFound("Team member not found")
    sessions = await _sessions(db, [user_id], start, end)
    events = await db.scalars(
        select(ActivityEvent)
        .where(
            ActivityEvent.user_id == user_id,
            ActivityEvent.created_at >= start,
            ActivityEvent.created_at < end,
        )
        .order_by(ActivityEvent.created_at.desc())
        .limit(200)
    )
    return AttendanceDetailOut(
        user=report.rows[0],
        sessions=[WorkSessionOut.model_validate(s) for s in reversed(sessions)],
        events=[ActivityEventOut.model_validate(e) for e in events],
    )


async def working_now_ids(db: AsyncSession, user_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not user_ids:
        return set()
    rows = await db.scalars(
        select(WorkSession.user_id).where(
            WorkSession.user_id.in_(user_ids), WorkSession.ended_at.is_(None)
        )
    )
    return set(rows)
