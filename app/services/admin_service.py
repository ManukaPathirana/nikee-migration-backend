"""Admin: staff and customer management, dashboard stats, visa type configuration."""

import re
import uuid
from datetime import timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import BadRequest, Conflict, NotFound
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
    WeekCount,
)
from app.schemas.common import PageParams
from app.schemas.inquiry import InquiryOut
from app.schemas.user import CustomerProfileOut
from app.services import application_service, auth_service
from app.services.audit_service import audit
from app.services.auth_service import get_user_by_email, normalize_email, revoke_all_refresh_tokens

OPEN_INQUIRY = (InquiryStatus.SCHEDULED, InquiryStatus.PROCESSING)


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


def _staff_out(user: User, open_count: int) -> StaffOut:
    profile = user.staff_profile
    return StaffOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone=user.phone,
        designation=profile.designation if profile else None,
        specialization=profile.specialization if profile else None,
        is_active=user.is_active,
        invite_pending=user.password_hash is None,
        open_inquiries=open_count,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


async def list_staff(
    db: AsyncSession, params: PageParams, *, q: str | None, active: bool | None
) -> tuple[list[StaffOut], int]:
    query = _search(select(User).where(User.role == UserRole.STAFF), q)
    if active is not None:
        query = query.where(User.is_active.is_(active))
    rows, total = await _page(db, query, params, User.full_name)
    counts = await _open_counts(db, [u.id for u in rows])
    return [_staff_out(u, counts.get(u.id, 0)) for u in rows], total


async def get_staff(db: AsyncSession, staff_id: uuid.UUID) -> User:
    user = await db.get(User, staff_id)
    if user is None or user.role != UserRole.STAFF:
        raise NotFound("Staff member not found")
    return user


async def staff_out(db: AsyncSession, user: User) -> StaffOut:
    counts = await _open_counts(db, [user.id])
    return _staff_out(user, counts.get(user.id, 0))


async def create_staff(
    db: AsyncSession,
    admin: User,
    *,
    email: str,
    full_name: str,
    phone: str | None,
    designation: str | None,
    specialization: str | None,
) -> User:
    email = normalize_email(email)
    if await get_user_by_email(db, email) is not None:
        raise Conflict("An account with this email already exists", code="email_taken")
    user = User(
        email=email,
        password_hash=None,
        role=UserRole.STAFF,
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
        meta={"email": email},
    )
    await auth_service.issue_invite(db, user, invited_by=admin)  # commits + emails
    return user


async def update_staff(db: AsyncSession, admin: User, user: User, changes: dict) -> None:
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
    if user.role == UserRole.ADMIN:
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


async def dashboard(db: AsyncSession) -> DashboardOut:
    now = utcnow()

    async def count(query) -> int:
        return (await db.scalar(query)) or 0

    customers_total = await count(
        select(func.count()).select_from(User).where(User.role == UserRole.CUSTOMER)
    )
    customers_new = await count(
        select(func.count())
        .select_from(User)
        .where(User.role == UserRole.CUSTOMER, User.created_at >= now - timedelta(days=30))
    )
    staff_active = await count(
        select(func.count())
        .select_from(User)
        .where(User.role == UserRole.STAFF, User.is_active.is_(True))
    )

    by_status = {s: 0 for s in InquiryStatus}
    for status, n in (
        await db.execute(select(Inquiry.status, func.count()).group_by(Inquiry.status))
    ).all():
        by_status[status] = n
    unassigned = await count(
        select(func.count())
        .select_from(Inquiry)
        .where(Inquiry.status.in_(OPEN_INQUIRY), Inquiry.assigned_staff_id.is_(None))
    )

    # Group in UTC (not the DB server's timezone) so weeks start on Monday 00:00 UTC.
    week = func.date_trunc("week", func.timezone("UTC", Inquiry.created_at))
    weeks_back = now - timedelta(weeks=8)
    weekly_rows = (
        await db.execute(
            select(week, func.count())
            .where(Inquiry.created_at >= weeks_back)
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
            select(VisaApplication.status, func.count()).group_by(VisaApplication.status)
        )
    ).all():
        apps_by_status[status] = n
    docs_pending = await count(
        select(func.count())
        .select_from(ApplicationDocument)
        .join(VisaApplication, VisaApplication.id == ApplicationDocument.application_id)
        .where(
            ApplicationDocument.status == DocumentStatus.PENDING,
            VisaApplication.status.in_([ApplicationStatus.SUBMITTED, ApplicationStatus.IN_REVIEW]),
        )
    )

    meetings_upcoming = await count(
        select(func.count())
        .select_from(Meeting)
        .join(MeetingSlot, MeetingSlot.id == Meeting.confirmed_slot_id)
        .where(
            Meeting.status == MeetingStatus.CONFIRMED,
            MeetingSlot.start_at >= now,
            MeetingSlot.start_at < now + timedelta(days=7),
        )
    )
    meetings_awaiting = await count(
        select(func.count()).select_from(Meeting).where(Meeting.status == MeetingStatus.PROPOSED)
    )

    reviews_count, reviews_avg = (
        await db.execute(select(func.count(Review.id), func.avg(Review.rating)))
    ).one()
    reviews_flagged = await count(
        select(func.count()).select_from(Review).where(Review.flagged.is_(True))
    )

    activity_rows = (
        await db.execute(
            select(AuditLog, User.full_name)
            .outerjoin(User, User.id == AuditLog.actor_id)
            .order_by(AuditLog.created_at.desc())
            .limit(10)
        )
    ).all()

    return DashboardOut(
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
