"""Team admin's live view of every staff member's visa cases."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import team_scope
from app.core.security import utcnow
from app.models import (
    AuditLog,
    CaseStage,
    DocumentStatus,
    Invoice,
    InvoiceStatus,
    User,
    UserRole,
    VisaApplication,
    VisaType,
)
from app.schemas.admin import StaffWork, TeamOverviewOut, WaitingItem
from app.schemas.inquiry import TeamBrief
from app.services import attendance_service
from app.services.application_service import base_query, stage_of, waiting_on

OVERDUE_AFTER = timedelta(hours=48)

PHASE = {
    CaseStage.CONTRACT_PENDING: "new",
    CaseStage.AGREEMENT: "agreement",
    CaseStage.AGREEMENT_REVIEW: "agreement",
    CaseStage.DOCUMENTS: "documents",
    CaseStage.FINAL_PAYMENT: "payment",
    CaseStage.CONFIRMATION_PENDING: "confirmation",
    CaseStage.CONFIRMATION: "confirmation",
    CaseStage.CONFIRMATION_REVIEW: "confirmation",
}
PHASES = ["new", "agreement", "documents", "payment", "confirmation"]

ACTION_TEXT = {
    "case.send_agreement": "sent a contract and deposit invoice",
    "case.approve_agreement": "approved a signed contract",
    "case.recall_contract": "recalled a contract",
    "case.finalize": "finalized an application",
    "case.send_confirmation": "sent a data confirmation",
    "case.recall_confirmation": "recalled a data confirmation",
    "case.complete": "completed a case",
    "document.approved": "approved a document",
    "document.rejected": "recalled a document",
    "inquiry.status": "updated an inquiry",
    "meeting.propose": "proposed meeting times",
}


def _task(app: VisaApplication) -> str | None:
    stage = stage_of(app)
    if stage == CaseStage.CONTRACT_PENDING:
        return "Send contract and deposit invoice"
    if stage == CaseStage.AGREEMENT_REVIEW:
        return "Review signed contract and deposit"
    if stage == CaseStage.DOCUMENTS:
        pending = sum(1 for d in app.documents if d.status == DocumentStatus.PENDING)
        if pending:
            return f"Review {pending} document{'s' if pending != 1 else ''}"
        return None
    if stage == CaseStage.CONFIRMATION_PENDING:
        return "Send final application and data confirmation"
    if stage == CaseStage.CONFIRMATION_REVIEW:
        return "Check signed data confirmation"
    return None


async def team_overview(
    db: AsyncSession, actor: User, team_id: uuid.UUID | None = None
) -> TeamOverviewOut:
    scope = team_scope(actor) or team_id
    now = utcnow()
    team = await db.get(VisaType, scope) if scope else None

    open_stages = [s.value for s in PHASE] + [CaseStage.ASSIGNMENT.value]
    q = base_query().where(VisaApplication.stage.in_(open_stages))
    if scope:
        q = q.where(VisaApplication.visa_type_id == scope)
    cases = list(await db.scalars(q))

    staff_q = select(User).where(User.role == UserRole.STAFF, User.is_active.is_(True))
    if scope:
        staff_q = staff_q.where(User.team_id == scope)
    staff = list(await db.scalars(staff_q.order_by(User.full_name)))
    ids = [u.id for u in staff]

    sessions = await attendance_service._sessions(
        db, ids, attendance_service.day_start(now), now + timedelta(seconds=1)
    )
    open_since = {s.user_id: s.started_at for s in sessions if s.ended_at is None}
    by_user_sessions: dict[uuid.UUID, list] = {}
    for s in sessions:
        by_user_sessions.setdefault(s.user_id, []).append(s)

    last_actions: dict[uuid.UUID, tuple[str, object]] = {}
    if ids:
        latest = (
            select(AuditLog.actor_id, func.max(AuditLog.created_at).label("at"))
            .where(AuditLog.actor_id.in_(ids))
            .group_by(AuditLog.actor_id)
            .subquery()
        )
        rows = await db.execute(
            select(AuditLog.actor_id, AuditLog.action, AuditLog.created_at).join(
                latest,
                (latest.c.actor_id == AuditLog.actor_id) & (latest.c.at == AuditLog.created_at),
            )
        )
        for uid, action, at in rows:
            last_actions[uid] = (ACTION_TEXT.get(action, action.replace(".", " ")), at)

    week = attendance_service.week_start(now)
    paid_q = (
        select(func.coalesce(func.sum(Invoice.amount_cents), 0))
        .join(VisaApplication, VisaApplication.id == Invoice.application_id)
        .where(Invoice.status == InvoiceStatus.PAID.value, Invoice.paid_at >= week)
    )
    if scope:
        paid_q = paid_q.where(VisaApplication.visa_type_id == scope)
    paid_week = int(await db.scalar(paid_q) or 0)

    rows_out: list[StaffWork] = []
    for u in staff:
        mine = [a for a in cases if a.assigned_staff_id == u.id]
        phases = dict.fromkeys(PHASES, 0)
        waiting: list[WaitingItem] = []
        for a in mine:
            phases[PHASE[stage_of(a)]] += 1
            task = _task(a)
            if task:
                since = a.stage_changed_at
                waiting.append(
                    WaitingItem(
                        application_id=a.id,
                        ref_no=a.ref_no,
                        customer_name=a.customer.full_name,
                        task=task,
                        since=since,
                        overdue=since is not None and now - since > OVERDUE_AFTER,
                    )
                )
        waiting.sort(key=lambda w: w.since or now)
        last = last_actions.get(u.id)
        rows_out.append(
            StaffWork(
                user_id=u.id,
                full_name=u.full_name,
                email=u.email,
                team=TeamBrief.model_validate(u.team) if u.team else None,
                working_now=u.id in open_since,
                working_since=open_since.get(u.id),
                today_seconds=attendance_service.overlap_seconds(
                    by_user_sessions.get(u.id, []), attendance_service.day_start(now), now, now
                ),
                cases_total=len(mine),
                by_phase=phases,
                waiting=waiting,
                overdue=sum(1 for w in waiting if w.overdue),
                last_action=last[0] if last else None,
                last_action_at=last[1] if last else None,
            )
        )
    # Busiest and most overdue first, so problems are at the top.
    rows_out.sort(key=lambda r: (-r.overdue, -len(r.waiting), r.full_name))

    return TeamOverviewOut(
        team=TeamBrief.model_validate(team) if team else None,
        open_cases=len(cases),
        unassigned=sum(1 for a in cases if stage_of(a) == CaseStage.ASSIGNMENT),
        waiting_on_staff=sum(1 for a in cases if waiting_on(a) == "staff"),
        waiting_on_customer=sum(1 for a in cases if waiting_on(a) == "customer"),
        paid_this_week_cents=paid_week,
        staff=rows_out,
    )
