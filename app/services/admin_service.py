"""Admin: staff and customer management, dashboard stats, visa type configuration."""

import re
import uuid
from datetime import timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.core.permissions import can_access_team, is_super_admin, team_scope
from app.core.security import utcnow
from app.models import (
    ApplicationDocument,
    ApplicationStatus,
    AuditLog,
    CustomerProfile,
    DocumentRequirement,
    DocumentStatus,
    Inquiry,
    InquiryStatus,
    Meeting,
    MeetingSlot,
    MeetingStatus,
    Review,
    StaffProfile,
    User,
    UserRole,
    VisaApplication,
    VisaType,
)
from app.schemas.admin import (
    ActivityOut,
    CustomerDetail,
    CustomerOut,
    DashboardOut,
    StaffOut,
    TeamStat,
    WeekCount,
)
from app.schemas.common import PageParams
from app.schemas.inquiry import InquiryOut, TeamBrief
from app.schemas.user import CustomerProfileOut
from app.services import application_service, attendance_service, auth_service
from app.services.audit_service import audit
from app.services.auth_service import get_user_by_email, normalize_email, revoke_all_refresh_tokens

OPEN_INQUIRY = (InquiryStatus.SCHEDULED, InquiryStatus.PROCESSING)
TEAM_ROLES = (UserRole.STAFF, UserRole.ADMIN)


def _search(query: Select, q: str | None) -> Select:
    if not q:
        return query
    like = f"%{q.strip()}%"
    return query.where(
        or_(User.full_name.ilike(like), User.email.ilike(like), User.phone.ilike(like))
    )


async def _page(db: AsyncSession, query: Select, params: PageParams, order) -> tuple[list, int]:
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = await db.scalars(query.order_by(order).offset(params.offset).limit(params.page_size))
    return list(rows), total or 0


# ---------------------------------------------------------------- staff


async def _open_counts(db: AsyncSession, staff_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not staff_ids:
        return {}
    rows = await db.execute(
        select(Inquiry.assigned_staff_id, func.count())
        .where(Inquiry.assigned_staff_id.in_(staff_ids), Inquiry.status.in_(OPEN_INQUIRY))
        .group_by(Inquiry.assigned_staff_id)
    )
    return dict(rows.all())


def _staff_out(user: User, open_count: int, working: bool) -> StaffOut:
    profile = user.staff_profile
    return StaffOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone=user.phone,
        role=user.role,
        team=TeamBrief.model_validate(user.team) if user.team else None,
        designation=profile.designation if profile else None,
        specialization=profile.specialization if profile else None,
        is_active=user.is_active,
        invite_pending=user.password_hash is None,
        open_inquiries=open_count,
        working_now=working,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


async def _staff_outs(db: AsyncSession, users: list[User]) -> list[StaffOut]:
    ids = [u.id for u in users]
    counts = await _open_counts(db, ids)
    working = await attendance_service.working_now_ids(db, ids)
    return [_staff_out(u, counts.get(u.id, 0), u.id in working) for u in users]


async def list_staff(
    db: AsyncSession,
    actor: User,
    params: PageParams,
    *,
    q: str | None,
    active: bool | None,
    team_id: uuid.UUID | None = None,
    role: UserRole | None = None,
) -> tuple[list[StaffOut], int]:
    """Super admin: staff and team admins of every team. Team admin: their own staff."""
    if is_super_admin(actor):
        roles = [role] if role in TEAM_ROLES else list(TEAM_ROLES)
    else:
        roles = [UserRole.STAFF]
    query = _search(select(User).where(User.role.in_(roles)), q)
    scope = team_scope(actor) or team_id
    if scope is not None:
        query = query.where(User.team_id == scope)
    if active is not None:
        query = query.where(User.is_active.is_(active))
    rows, total = await _page(db, query, params, User.full_name)
    return await _staff_outs(db, rows), total


async def get_staff(db: AsyncSession, actor: User, staff_id: uuid.UUID) -> User:
    user = await db.get(User, staff_id)
    if user is None or user.role not in TEAM_ROLES or not can_access_team(actor, user.team_id):
        raise NotFound("Staff member not found")
    if not is_super_admin(actor) and user.role == UserRole.ADMIN:
        raise NotFound("Staff member not found")  # team admins manage staff only
    return user


async def staff_out(db: AsyncSession, user: User) -> StaffOut:
    await db.refresh(user, ["team", "staff_profile"])  # new account or team just changed
    return (await _staff_outs(db, [user]))[0]


async def _active_team(db: AsyncSession, team_id: uuid.UUID | None) -> VisaType:
    if team_id is None:
        raise BadRequest("Choose a team", code="team_required")
    team = await db.get(VisaType, team_id)
    if team is None:
        raise BadRequest("That team doesn't exist", code="team_not_found")
    return team


async def create_staff(
    db: AsyncSession,
    admin: User,
    *,
    email: str,
    full_name: str,
    phone: str | None,
    designation: str | None,
    specialization: str | None,
    role: UserRole = UserRole.STAFF,
    team_id: uuid.UUID | None = None,
) -> User:
    if is_super_admin(admin):
        team = await _active_team(db, team_id)
    else:
        # Team admins can only add staff, and only to their own team.
        if role != UserRole.STAFF:
            raise Forbidden("Only the super admin can add team admins", code="super_admin_only")
        if team_id is not None and team_id != admin.team_id:
            raise Forbidden("You can only add staff to your own team", code="other_team")
        team = await _active_team(db, admin.team_id)
    email = normalize_email(email)
    if await get_user_by_email(db, email) is not None:
        raise Conflict("An account with this email already exists", code="email_taken")
    user = User(
        email=email,
        password_hash=None,
        role=role,
        team_id=team.id,
        full_name=full_name.strip(),
        phone=phone,
        is_verified=False,
    )
    user.staff_profile = StaffProfile(
        designation=designation, specialization=specialization, created_by=admin.id
    )
    db.add(user)
    await db.flush()
    audit(
        db,
        actor_id=admin.id,
        action="staff.create",
        entity="user",
        entity_id=user.id,
        meta={"email": email, "role": role.value, "team": team.slug},
    )
    await auth_service.issue_invite(db, user, invited_by=admin)  # commits + emails
    return user


async def update_staff(db: AsyncSession, admin: User, user: User, changes: dict) -> None:
    new_team = changes.pop("team_id", None)
    if new_team is not None and new_team != user.team_id:
        if not is_super_admin(admin):
            raise Forbidden(
                "Only the super admin can move people between teams", code="super_admin_only"
            )
        await _active_team(db, new_team)
        open_count = (await _open_counts(db, [user.id])).get(user.id, 0)
        if open_count:
            raise BadRequest(
                f"Reassign their {open_count} open inquiries before moving them to another team",
                code="has_open_inquiries",
            )
        user.team_id = new_team
        changes["team_id"] = new_team
    for field in ("full_name", "phone"):
        if field in changes:
            setattr(user, field, changes[field])
    profile = user.staff_profile or StaffProfile(user_id=user.id, created_by=admin.id)
    for field in ("designation", "specialization"):
        if field in changes:
            setattr(profile, field, changes[field])
    user.staff_profile = profile
    audit(
        db,
        actor_id=admin.id,
        action="staff.update",
        entity="user",
        entity_id=user.id,
        meta={"fields": sorted(changes)},
    )
    await db.commit()


async def set_active(db: AsyncSession, admin: User, user: User, active: bool) -> None:
    if user.id == admin.id:
        raise BadRequest("You can't deactivate your own account", code="self_deactivate")
    if user.role == UserRole.SUPER_ADMIN or (
        user.role == UserRole.ADMIN and not is_super_admin(admin)
    ):
        raise BadRequest("Admin accounts can't be changed here", code="admin_account")
    user.is_active = active
    if not active:
        await revoke_all_refresh_tokens(db, user.id)
    audit(
        db,
        actor_id=admin.id,
        action=f"{user.role.value}.{'activate' if active else 'deactivate'}",
        entity="user",
        entity_id=user.id,
    )
    await db.commit()


async def resend_invite(db: AsyncSession, admin: User, user: User) -> None:
    if user.password_hash is not None:
        raise BadRequest("This staff member has already set a password", code="invite_accepted")
    if not user.is_active:
        raise BadRequest("Activate the account before re-sending the invite", code="inactive")
    audit(db, actor_id=admin.id, action="staff.resend_invite", entity="user", entity_id=user.id)
    await auth_service.issue_invite(db, user, invited_by=admin)


# ---------------------------------------------------------------- customers


async def _customer_counts(
    db: AsyncSession, ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    if not ids:
        return {}, {}
    apps = await db.execute(
        select(VisaApplication.customer_id, func.count())
        .where(VisaApplication.customer_id.in_(ids))
        .group_by(VisaApplication.customer_id)
    )
    inqs = await db.execute(
        select(Inquiry.customer_id, func.count())
        .where(Inquiry.customer_id.in_(ids))
        .group_by(Inquiry.customer_id)
    )
    return dict(apps.all()), dict(inqs.all())


def _customer_out(u: User, apps: dict, inqs: dict) -> CustomerOut:
    return CustomerOut(
        id=u.id,
        email=u.email,
        full_name=u.full_name,
        phone=u.phone,
        is_active=u.is_active,
        is_verified=u.is_verified,
        applications_count=apps.get(u.id, 0),
        inquiries_count=inqs.get(u.id, 0),
        last_login_at=u.last_login_at,
        created_at=u.created_at,
    )


async def list_customers(
    db: AsyncSession, params: PageParams, *, q: str | None, active: bool | None
) -> tuple[list[CustomerOut], int]:
    query = _search(select(User).where(User.role == UserRole.CUSTOMER), q)
    if active is not None:
        query = query.where(User.is_active.is_(active))
    rows, total = await _page(db, query, params, User.created_at.desc())
    apps, inqs = await _customer_counts(db, [u.id for u in rows])
    return [_customer_out(u, apps, inqs) for u in rows], total


async def get_customer(db: AsyncSession, customer_id: uuid.UUID) -> User:
    user = await db.get(User, customer_id)
    if user is None or user.role != UserRole.CUSTOMER:
        raise NotFound("Customer not found")
    return user


async def customer_detail(db: AsyncSession, user: User) -> CustomerDetail:
    apps = await db.scalars(
        application_service.base_query()
        .where(VisaApplication.customer_id == user.id)
        .order_by(VisaApplication.created_at.desc())
    )
    inquiries = await db.scalars(
        select(Inquiry)
        .where(Inquiry.customer_id == user.id)
        .options(selectinload(Inquiry.customer), selectinload(Inquiry.assigned_staff))
        .order_by(Inquiry.created_at.desc())
    )
    app_counts, inq_counts = await _customer_counts(db, [user.id])
    profile = user.customer_profile or CustomerProfile()
    return CustomerDetail(
        **_customer_out(user, app_counts, inq_counts).model_dump(),
        profile=CustomerProfileOut.model_validate(profile),
        applications=[application_service.to_summary(a) for a in apps],
        inquiries=[InquiryOut.model_validate(i) for i in inquiries],
    )


# ---------------------------------------------------------------- dashboard


async def _team_stats(db: AsyncSession) -> list[TeamStat]:
    """Per-team overview for the super admin."""
    teams = list(await db.scalars(select(VisaType).order_by(VisaType.sort_order, VisaType.name)))
    by_status: dict[uuid.UUID, dict[InquiryStatus, int]] = {
        t.id: {s: 0 for s in InquiryStatus} for t in teams
    }
    for team_id, status, n in (
        await db.execute(
            select(Inquiry.visa_type_id, Inquiry.status, func.count()).group_by(
                Inquiry.visa_type_id, Inquiry.status
            )
        )
    ).all():
        if team_id in by_status:
            by_status[team_id][status] = n
    unassigned = dict(
        (
            await db.execute(
                select(Inquiry.visa_type_id, func.count())
                .where(Inquiry.status.in_(OPEN_INQUIRY), Inquiry.assigned_staff_id.is_(None))
                .group_by(Inquiry.visa_type_id)
            )
        ).all()
    )
    open_apps = dict(
        (
            await db.execute(
                select(VisaApplication.visa_type_id, func.count())
                .where(
                    VisaApplication.status.in_(
                        [ApplicationStatus.SUBMITTED, ApplicationStatus.IN_REVIEW]
                    )
                )
                .group_by(VisaApplication.visa_type_id)
            )
        ).all()
    )
    docs = dict(
        (
            await db.execute(
                select(VisaApplication.visa_type_id, func.count())
                .select_from(ApplicationDocument)
                .join(VisaApplication, VisaApplication.id == ApplicationDocument.application_id)
                .where(
                    ApplicationDocument.status == DocumentStatus.PENDING,
                    VisaApplication.status.in_(
                        [ApplicationStatus.SUBMITTED, ApplicationStatus.IN_REVIEW]
                    ),
                )
                .group_by(VisaApplication.visa_type_id)
            )
        ).all()
    )
    members = list(
        await db.scalars(select(User).where(User.role.in_(TEAM_ROLES), User.is_active.is_(True)))
    )
    working = await attendance_service.working_now_ids(db, [u.id for u in members])
    return [
        TeamStat(
            team=TeamBrief.model_validate(t),
            inquiries_by_status=by_status[t.id],
            inquiries_unassigned=unassigned.get(t.id, 0),
            applications_open=open_apps.get(t.id, 0),
            documents_pending=docs.get(t.id, 0),
            staff_active=sum(1 for u in members if u.team_id == t.id and u.role == UserRole.STAFF),
            working_now=sum(1 for u in members if u.team_id == t.id and u.id in working),
            admins=sorted(
                u.full_name for u in members if u.team_id == t.id and u.role == UserRole.ADMIN
            ),
        )
        for t in teams
    ]


async def dashboard(
    db: AsyncSession, actor: User, team_id: uuid.UUID | None = None
) -> DashboardOut:
    """Super admin: whole company (optionally one team). Team admin: their team only."""
    now = utcnow()
    scope = team_scope(actor) or team_id
    team = await db.get(VisaType, scope) if scope else None

    async def count(query) -> int:
        return (await db.scalar(query)) or 0

    def inq(query):
        return query.where(Inquiry.visa_type_id == scope) if scope else query

    def app(query):
        return query.where(VisaApplication.visa_type_id == scope) if scope else query

    if scope:
        # A team's customers: anyone with an inquiry or application in that team.
        team_customers = (
            select(Inquiry.customer_id.label("id"))
            .where(Inquiry.visa_type_id == scope)
            .union(select(VisaApplication.customer_id).where(VisaApplication.visa_type_id == scope))
            .subquery()
        )
        customers = (
            select(func.count()).select_from(User).where(User.id.in_(select(team_customers.c.id)))
        )
    else:
        customers = select(func.count()).select_from(User).where(User.role == UserRole.CUSTOMER)
    customers_total = await count(customers)
    customers_new = await count(customers.where(User.created_at >= now - timedelta(days=30)))

    members_q = select(User).where(User.role.in_(TEAM_ROLES), User.is_active.is_(True))
    if scope:
        members_q = members_q.where(User.team_id == scope)
    members = list(await db.scalars(members_q))
    staff_active = sum(1 for u in members if u.role == UserRole.STAFF)
    working_now = len(await attendance_service.working_now_ids(db, [u.id for u in members]))

    by_status = {s: 0 for s in InquiryStatus}
    for status, n in (
        await db.execute(inq(select(Inquiry.status, func.count())).group_by(Inquiry.status))
    ).all():
        by_status[status] = n
    unassigned = await count(
        inq(
            select(func.count())
            .select_from(Inquiry)
            .where(Inquiry.status.in_(OPEN_INQUIRY), Inquiry.assigned_staff_id.is_(None))
        )
    )

    # Group in UTC (not the DB server's timezone) so weeks start on Monday 00:00 UTC.
    week = func.date_trunc("week", func.timezone("UTC", Inquiry.created_at))
    weeks_back = now - timedelta(weeks=8)
    weekly_rows = (
        await db.execute(
            inq(select(week, func.count()).where(Inquiry.created_at >= weeks_back))
            .group_by(week)
            .order_by(week)
        )
    ).all()
    weekly_map = {w.date(): n for w, n in weekly_rows}
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    per_week = [
        WeekCount(
            week_start=monday - timedelta(weeks=i),
            count=weekly_map.get((monday - timedelta(weeks=i)).date(), 0),
        )
        for i in range(7, -1, -1)
    ]

    apps_by_status = {s: 0 for s in ApplicationStatus}
    for status, n in (
        await db.execute(
            app(select(VisaApplication.status, func.count())).group_by(VisaApplication.status)
        )
    ).all():
        apps_by_status[status] = n
    docs_pending = await count(
        app(
            select(func.count())
            .select_from(ApplicationDocument)
            .join(VisaApplication, VisaApplication.id == ApplicationDocument.application_id)
            .where(
                ApplicationDocument.status == DocumentStatus.PENDING,
                VisaApplication.status.in_(
                    [ApplicationStatus.SUBMITTED, ApplicationStatus.IN_REVIEW]
                ),
            )
        )
    )

    meetings_upcoming = await count(
        inq(
            select(func.count())
            .select_from(Meeting)
            .join(MeetingSlot, MeetingSlot.id == Meeting.confirmed_slot_id)
            .join(Inquiry, Inquiry.id == Meeting.inquiry_id)
            .where(
                Meeting.status == MeetingStatus.CONFIRMED,
                MeetingSlot.start_at >= now,
                MeetingSlot.start_at < now + timedelta(days=7),
            )
        )
    )
    meetings_awaiting = await count(
        inq(
            select(func.count())
            .select_from(Meeting)
            .join(Inquiry, Inquiry.id == Meeting.inquiry_id)
            .where(Meeting.status == MeetingStatus.PROPOSED)
        )
    )

    reviews_count, reviews_avg = (
        await db.execute(
            inq(
                select(func.count(Review.id), func.avg(Review.rating)).join(
                    Inquiry, Inquiry.id == Review.inquiry_id
                )
            )
        )
    ).one()
    reviews_flagged = await count(
        inq(
            select(func.count())
            .select_from(Review)
            .join(Inquiry, Inquiry.id == Review.inquiry_id)
            .where(Review.flagged.is_(True))
        )
    )

    activity_q = select(AuditLog, User.full_name).outerjoin(User, User.id == AuditLog.actor_id)
    if scope:  # what the team's own people did
        activity_q = activity_q.where(User.team_id == scope)
    activity_rows = (
        await db.execute(activity_q.order_by(AuditLog.created_at.desc()).limit(10))
    ).all()

    return DashboardOut(
        scope="all" if is_super_admin(actor) and team_id is None else "team",
        team=TeamBrief.model_validate(team) if team else None,
        working_now=working_now,
        teams=await _team_stats(db) if is_super_admin(actor) and team_id is None else [],
        customers_total=customers_total,
        customers_new_30d=customers_new,
        staff_active=staff_active,
        inquiries_by_status=by_status,
        inquiries_unassigned=unassigned,
        inquiries_per_week=per_week,
        applications_by_status=apps_by_status,
        documents_pending=docs_pending,
        meetings_upcoming_7d=meetings_upcoming,
        meetings_awaiting_customer=meetings_awaiting,
        reviews_count=reviews_count or 0,
        reviews_average=round(float(reviews_avg), 2) if reviews_avg is not None else None,
        reviews_flagged=reviews_flagged,
        recent_activity=[
            ActivityOut(
                id=log.id,
                actor_name=name,
                action=log.action,
                entity=log.entity,
                entity_id=log.entity_id,
                meta=log.meta,
                created_at=log.created_at,
            )
            for log, name in activity_rows
        ],
    )


# ---------------------------------------------------------------- visa types


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:120] or "visa"


async def list_visa_types(db: AsyncSession) -> list[VisaType]:
    rows = await db.scalars(
        select(VisaType)
        .options(selectinload(VisaType.requirements))
        .order_by(VisaType.sort_order, VisaType.name)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def get_visa_type(db: AsyncSession, visa_id: uuid.UUID) -> VisaType:
    visa = await db.scalar(
        select(VisaType)
        .where(VisaType.id == visa_id)
        .options(selectinload(VisaType.requirements))
        .execution_options(populate_existing=True)
    )
    if visa is None:
        raise NotFound("Visa type not found")
    return visa


async def create_visa_type(db: AsyncSession, admin: User, data: dict) -> VisaType:
    slug = data.pop("slug", None) or _slugify(data["name"])
    if await db.scalar(select(VisaType.id).where(VisaType.slug == slug)):
        raise Conflict("A visa type with this name already exists", code="slug_taken")
    visa = VisaType(slug=slug, **data)
    db.add(visa)
    await db.flush()
    audit(db, actor_id=admin.id, action="visa_type.create", entity="visa_type", entity_id=visa.id)
    await db.commit()
    return await get_visa_type(db, visa.id)


async def update_visa_type(
    db: AsyncSession, admin: User, visa: VisaType, changes: dict
) -> VisaType:
    for field, value in changes.items():
        setattr(visa, field, value)
    audit(
        db,
        actor_id=admin.id,
        action="visa_type.update",
        entity="visa_type",
        entity_id=visa.id,
        meta={"fields": sorted(changes)},
    )
    await db.commit()
    return await get_visa_type(db, visa.id)


async def add_requirement(db: AsyncSession, admin: User, visa: VisaType, data: dict) -> VisaType:
    if data.get("sort_order") is None:
        data["sort_order"] = (max((r.sort_order for r in visa.requirements), default=-1)) + 1
    req = DocumentRequirement(visa_type_id=visa.id, **data)
    db.add(req)
    await db.flush()
    audit(
        db,
        actor_id=admin.id,
        action="requirement.create",
        entity="document_requirement",
        entity_id=req.id,
        meta={"visa_type_id": str(visa.id)},
    )
    await db.commit()
    return await get_visa_type(db, visa.id)


async def get_requirement(db: AsyncSession, req_id: uuid.UUID) -> DocumentRequirement:
    req = await db.get(DocumentRequirement, req_id)
    if req is None:
        raise NotFound("Document requirement not found")
    return req


async def update_requirement(
    db: AsyncSession, admin: User, req: DocumentRequirement, changes: dict
) -> VisaType:
    for field, value in changes.items():
        setattr(req, field, value)
    audit(
        db,
        actor_id=admin.id,
        action="requirement.update",
        entity="document_requirement",
        entity_id=req.id,
        meta={"fields": sorted(changes)},
    )
    await db.commit()
    return await get_visa_type(db, req.visa_type_id)


async def delete_requirement(db: AsyncSession, admin: User, req: DocumentRequirement) -> VisaType:
    visa_id = req.visa_type_id
    await db.delete(req)  # uploaded documents keep their file; requirement_id becomes NULL
    audit(
        db,
        actor_id=admin.id,
        action="requirement.delete",
        entity="document_requirement",
        entity_id=req.id,
        meta={"name": req.name},
    )
    await db.commit()
    return await get_visa_type(db, visa_id)
