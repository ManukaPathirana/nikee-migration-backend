"""Inquiries: reference numbers, status rules + history, claim/assign, notes, board."""

import uuid
from typing import Literal

from sqlalchemy import Select, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.core.permissions import can_access_team, is_admin, is_internal, team_scope
from app.core.security import utcnow
from app.models import (
    Inquiry,
    InquiryNote,
    InquiryStatus,
    InquiryStatusHistory,
    Meeting,
    MeetingStatus,
    User,
    UserRole,
    VisaApplication,
    VisaType,
)
from app.schemas.common import PageParams
from app.schemas.inquiry import InquiryDetail
from app.services import job_queue
from app.services.audit_service import audit
from app.services.notification_service import notify, notify_super_admins

S = InquiryStatus

# Normal flow (staff + admin).
TRANSITIONS: dict[InquiryStatus, set[InquiryStatus]] = {
    S.SCHEDULED: {S.PROCESSING, S.CANCELLED},
    S.PROCESSING: {S.COMPLETED, S.CANCELLED},
}
# Completed/cancelled are final; only admin may reopen.
REOPEN: dict[InquiryStatus, set[InquiryStatus]] = {
    S.COMPLETED: {S.PROCESSING, S.SCHEDULED},
    S.CANCELLED: {S.SCHEDULED, S.PROCESSING},
}
FINAL = {S.COMPLETED, S.CANCELLED}

Scope = Literal["all", "queue", "mine"]


def allowed_transitions(inquiry: Inquiry, user: User) -> set[InquiryStatus]:
    if inquiry.status in FINAL:
        return REOPEN[inquiry.status] if is_admin(user) else set()
    return TRANSITIONS.get(inquiry.status, set())


def _is_staff(user: User) -> bool:
    """Staff, team admin or super admin."""
    return is_internal(user)


def _query() -> Select[tuple[Inquiry]]:
    return (
        select(Inquiry)
        .options(joinedload(Inquiry.customer), joinedload(Inquiry.assigned_staff))
        .execution_options(populate_existing=True)
    )


def _detail_query() -> Select[tuple[Inquiry]]:
    return _query().options(
        selectinload(Inquiry.history).joinedload(InquiryStatusHistory.actor),
        selectinload(Inquiry.notes).joinedload(InquiryNote.author),
    )


def to_detail(inquiry: Inquiry, viewer: User) -> InquiryDetail:
    detail = InquiryDetail.model_validate(inquiry)
    if not _is_staff(viewer):
        detail.notes = [n for n in detail.notes if not n.is_internal]
    return detail


# ---------------------------------------------------------------- reads


async def get_inquiry(db: AsyncSession, inquiry_id: uuid.UUID, user: User) -> Inquiry:
    inquiry = await db.scalar(_detail_query().where(Inquiry.id == inquiry_id))
    if inquiry is None or (not _is_staff(user) and inquiry.customer_id != user.id):
        raise NotFound("Inquiry not found")
    if _is_staff(user) and not can_access_team(user, inquiry.visa_type_id):
        raise NotFound("Inquiry not found")  # another team's inquiry
    if user.role == UserRole.STAFF and inquiry.assigned_staff_id != user.id:
        raise NotFound("Inquiry not found")  # handled by the team admin
    return inquiry


async def list_inquiries(
    db: AsyncSession,
    user: User,
    params: PageParams,
    *,
    scope: Scope = "all",
    status: InquiryStatus | None = None,
    assigned_staff_id: uuid.UUID | None = None,
    search: str | None = None,
    team_id: uuid.UUID | None = None,
) -> tuple[list[Inquiry], int]:
    query = _query()
    if _is_staff(user):
        # Team members see their own team; the super admin may filter by team.
        scope_team = team_scope(user) or team_id
        if scope_team is not None:
            query = query.where(Inquiry.visa_type_id == scope_team)
    if user.role == UserRole.STAFF:
        # Only inquiries handed to a staff member earlier stay visible; new ones are the team
        # admin's (both kinds) and are never assigned to staff.
        query = query.where(Inquiry.assigned_staff_id == user.id)
    if not _is_staff(user):
        query = query.where(Inquiry.customer_id == user.id)
    elif scope == "queue":
        query = query.where(Inquiry.status == S.SCHEDULED, Inquiry.assigned_staff_id.is_(None))
    elif scope == "mine":
        query = query.where(Inquiry.assigned_staff_id == user.id)
    if status is not None:
        query = query.where(Inquiry.status == status)
    if assigned_staff_id is not None and _is_staff(user):
        query = query.where(Inquiry.assigned_staff_id == assigned_staff_id)
    if search:
        like = f"%{search.strip()}%"
        query = query.join(User, User.id == Inquiry.customer_id).where(
            or_(
                Inquiry.ref_no.ilike(like),
                Inquiry.subject.ilike(like),
                User.full_name.ilike(like),
                User.email.ilike(like),
            )
        )
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    # Queue: oldest first (first come, first served). Everything else: latest activity first.
    order = Inquiry.created_at.asc() if scope == "queue" else Inquiry.updated_at.desc()
    rows = await db.scalars(query.order_by(order).offset(params.offset).limit(params.page_size))
    return list(rows), total or 0


async def board(
    db: AsyncSession, team_id: uuid.UUID | None = None, per_column: int = 50
) -> dict[InquiryStatus, list[Inquiry]]:
    """Kanban columns (latest `per_column` per status) in one query; `team_id=None` = all teams."""
    ranked = select(
        Inquiry.id,
        func.row_number()
        .over(partition_by=Inquiry.status, order_by=Inquiry.updated_at.desc())
        .label("rn"),
    )
    if team_id is not None:
        ranked = ranked.where(Inquiry.visa_type_id == team_id)
    ranked = ranked.subquery()
    rows = await db.scalars(
        _query()
        .join(ranked, ranked.c.id == Inquiry.id)
        .where(ranked.c.rn <= per_column)
        .order_by(Inquiry.updated_at.desc())
    )
    columns: dict[InquiryStatus, list[Inquiry]] = {s: [] for s in InquiryStatus}
    for inquiry in rows.unique():
        columns[inquiry.status].append(inquiry)
    return columns


async def board_counts(
    db: AsyncSession, team_id: uuid.UUID | None = None
) -> dict[InquiryStatus, int]:
    query = select(Inquiry.status, func.count()).group_by(Inquiry.status)
    if team_id is not None:
        query = query.where(Inquiry.visa_type_id == team_id)
    rows = await db.execute(query)
    counts = {s: 0 for s in InquiryStatus}
    counts.update({status: n for status, n in rows.all()})
    return counts


# ---------------------------------------------------------------- create


async def _next_ref_no(db: AsyncSession) -> str:
    """NM-<year>-<NNNN>, sequential per year. An advisory lock serialises concurrent creates."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('inquiry_ref_no'))"))
    prefix = f"NM-{utcnow().year}-"
    last = await db.scalar(
        select(Inquiry.ref_no)
        .where(Inquiry.ref_no.like(f"{prefix}%"))
        .order_by(func.length(Inquiry.ref_no).desc(), Inquiry.ref_no.desc())
        .limit(1)
    )
    number = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{number:04d}"


async def create_inquiry(
    db: AsyncSession,
    customer: User,
    *,
    subject: str,
    category: str,
    description: str,
    preferred_contact_time: str | None,
    application_id: uuid.UUID | None,
    visa_type_id: uuid.UUID,
    kind: str = "consultation",
) -> Inquiry:
    visa = await db.get(VisaType, visa_type_id)
    if visa is None or not visa.is_active:
        raise BadRequest("Choose a visa type", code="invalid_visa_type")
    if application_id is not None:
        app = await db.get(VisaApplication, application_id)
        if app is None or app.customer_id != customer.id:
            raise BadRequest("Application not found", code="invalid_application")
        if app.visa_type_id != visa.id:
            raise BadRequest(
                "The linked application is for a different visa type", code="visa_type_mismatch"
            )
    inquiry = Inquiry(
        ref_no=await _next_ref_no(db),
        customer_id=customer.id,
        visa_type_id=visa.id,
        kind=kind,
        application_id=application_id,
        subject=subject.strip(),
        category=category,
        description=description.strip(),
        preferred_contact_time=(preferred_contact_time or "").strip() or None,
        status=S.SCHEDULED,
    )
    db.add(inquiry)
    await db.flush()
    db.add(
        InquiryStatusHistory(
            inquiry_id=inquiry.id,
            from_status=None,
            to_status=S.SCHEDULED,
            changed_by=customer.id,
            reason="Inquiry placed",
        )
    )
    # New inquiries go to the team admins of that visa type first; they assign a team member.
    team_admins = list(
        await db.scalars(
            select(User).where(
                User.role == UserRole.ADMIN, User.team_id == visa.id, User.is_active.is_(True)
            )
        )
    )
    for admin in team_admins:
        notify(
            db,
            user_id=admin.id,
            type="inquiry_new",
            title=f"New {visa.name} inquiry {inquiry.ref_no}",
            body=f"{customer.full_name}: {inquiry.subject}",
            data={"inquiry_id": str(inquiry.id)},
        )
    await notify_super_admins(
        db,
        type="inquiry_new",
        title=f"New {visa.name} inquiry {inquiry.ref_no}",
        body=f"{customer.full_name}: {inquiry.subject}",
        data={"inquiry_id": str(inquiry.id)},
    )
    await db.commit()
    for admin in team_admins:
        await job_queue.send_email(
            to=admin.email,
            subject=f"New {visa.name} inquiry {inquiry.ref_no}",
            template="inquiry_update.html",
            context={
                "name": admin.full_name,
                "heading": f"A new {visa.name} inquiry is waiting",
                "message": f"{customer.full_name} sent a new inquiry. Open the Inquiry Board "
                "to assign it to someone in your team.",
                "ref_no": inquiry.ref_no,
                "subject": inquiry.subject,
                "status": S.SCHEDULED.value,
            },
        )
    await job_queue.send_email(
        to=customer.email,
        subject=f"We've received your inquiry {inquiry.ref_no}",
        template="inquiry_update.html",
        context={
            "name": customer.full_name,
            "heading": "We've received your inquiry",
            "message": "Our team will pick it up shortly. You can follow its progress in the app.",
            "ref_no": inquiry.ref_no,
            "subject": inquiry.subject,
            "status": S.SCHEDULED.value,
        },
    )
    return await get_inquiry(db, inquiry.id, customer)


# ---------------------------------------------------------------- staff actions


async def claim(db: AsyncSession, inquiry: Inquiry, staff: User) -> None:
    if inquiry.assigned_staff_id != staff.id:
        raise BadRequest(
            "Inquiries are handled by the team admin; staff can't claim them",
            code="no_staff_assignment",
        )
    if inquiry.assigned_staff_id == staff.id:
        return
    if inquiry.assigned_staff_id is not None:
        raise Conflict(
            "Another team member has already claimed this inquiry", code="already_claimed"
        )
    if inquiry.status != S.SCHEDULED:
        raise BadRequest("Only scheduled inquiries can be claimed", code="invalid_status")
    inquiry.assigned_staff_id = staff.id
    audit(db, actor_id=staff.id, action="inquiry.claim", entity="inquiry", entity_id=inquiry.id)
    await db.commit()


async def assign(db: AsyncSession, inquiry: Inquiry, admin: User, staff_id: uuid.UUID) -> None:
    raise BadRequest(
        "Inquiries are handled by the team admin; they are not assigned to staff",
        code="no_staff_assignment",
    )
    staff = await db.get(User, staff_id)
    if staff is None or staff.role != UserRole.STAFF or not staff.is_active:
        raise BadRequest("Choose an active staff member", code="invalid_staff")
    if staff.team_id != inquiry.visa_type_id:
        raise BadRequest("Choose a staff member from this inquiry's team", code="wrong_team")
    previous = inquiry.assigned_staff_id
    inquiry.assigned_staff_id = staff.id
    # Open meetings follow the inquiry to the new consultant so they can run them.
    await db.execute(
        update(Meeting)
        .where(
            Meeting.inquiry_id == inquiry.id,
            Meeting.status.in_(
                [
                    MeetingStatus.PROPOSED,
                    MeetingStatus.CONFIRMED,
                    MeetingStatus.RESCHEDULE_REQUESTED,
                ]
            ),
        )
        .values(staff_id=staff.id)
    )
    notify(
        db,
        user_id=staff.id,
        type="inquiry_assigned",
        title=f"Inquiry {inquiry.ref_no} assigned to you",
        body=inquiry.subject,
        data={"inquiry_id": str(inquiry.id)},
    )
    audit(
        db,
        actor_id=admin.id,
        action="inquiry.assign",
        entity="inquiry",
        entity_id=inquiry.id,
        meta={"from": str(previous) if previous else None, "to": str(staff.id)},
    )
    await db.commit()
    await job_queue.send_email(
        to=staff.email,
        subject=f"Inquiry {inquiry.ref_no} assigned to you",
        template="inquiry_update.html",
        context={
            "name": staff.full_name,
            "heading": "A new inquiry is assigned to you",
            "message": f"{admin.full_name} assigned this inquiry to you.",
            "ref_no": inquiry.ref_no,
            "subject": inquiry.subject,
            "status": inquiry.status.value,
        },
    )


async def change_status(
    db: AsyncSession,
    inquiry: Inquiry,
    actor: User,
    new_status: InquiryStatus,
    reason: str | None,
) -> None:
    reason = (reason or "").strip() or None
    if new_status not in allowed_transitions(inquiry, actor):
        if inquiry.status in FINAL and not is_admin(actor):
            raise Forbidden(
                f"This inquiry is {inquiry.status.value}. Only an admin can reopen it.",
                code="inquiry_closed",
            )
        raise BadRequest(
            f"Can't change an inquiry from {inquiry.status.value} to {new_status.value}",
            code="invalid_transition",
        )
    if new_status == S.CANCELLED and not reason:
        raise BadRequest("A reason is required to cancel an inquiry", code="reason_required")
    if inquiry.status in FINAL and not reason:
        raise BadRequest("A reason is required to reopen an inquiry", code="reason_required")

    if actor.role == UserRole.STAFF:
        if inquiry.assigned_staff_id is None:
            inquiry.assigned_staff_id = actor.id  # acting on an unclaimed inquiry claims it
        elif inquiry.assigned_staff_id != actor.id:
            raise Forbidden("This inquiry is assigned to another team member", code="not_assigned")

    old = inquiry.status
    inquiry.status = new_status
    inquiry.closed_at = utcnow() if new_status in FINAL else None
    db.add(
        InquiryStatusHistory(
            inquiry_id=inquiry.id,
            from_status=old,
            to_status=new_status,
            changed_by=actor.id,
            reason=reason,
        )
    )
    label = new_status.value
    notify(
        db,
        user_id=inquiry.customer_id,
        type="inquiry_status",
        title=f"Inquiry {inquiry.ref_no} is now {label}",
        body=reason,
        data={"inquiry_id": str(inquiry.id), "status": label},
    )
    audit(
        db,
        actor_id=actor.id,
        action="inquiry.status",
        entity="inquiry",
        entity_id=inquiry.id,
        meta={"from": old.value, "to": label, "reason": reason},
    )
    await db.commit()

    messages = {
        S.PROCESSING: "A consultant is now working on your inquiry.",
        S.COMPLETED: "Your inquiry has been completed. We'd love to hear how we did — "
        "you can leave a review in the app.",
        S.CANCELLED: "Your inquiry has been cancelled.",
        S.SCHEDULED: "Your inquiry has been reopened and is back in our queue.",
    }
    await job_queue.send_email(
        to=inquiry.customer.email,
        subject=f"Inquiry {inquiry.ref_no}: {label}",
        template="inquiry_update.html",
        context={
            "name": inquiry.customer.full_name,
            "heading": f"Your inquiry is now {label}",
            "message": messages[new_status],
            "reason": reason,
            "ref_no": inquiry.ref_no,
            "subject": inquiry.subject,
            "status": label,
        },
    )


async def add_note(
    db: AsyncSession, inquiry: Inquiry, author: User, *, body: str, is_internal: bool
) -> InquiryNote:
    staff = _is_staff(author)
    if (
        staff
        and author.role == UserRole.STAFF
        and inquiry.assigned_staff_id not in (None, author.id)
    ):
        raise Forbidden("This inquiry is assigned to another team member", code="not_assigned")
    internal = is_internal if staff else False
    note = InquiryNote(
        inquiry_id=inquiry.id, author_id=author.id, body=body.strip(), is_internal=internal
    )
    db.add(note)
    inquiry.updated_at = utcnow()

    if staff and not internal:
        notify(
            db,
            user_id=inquiry.customer_id,
            type="inquiry_message",
            title=f"New message on {inquiry.ref_no}",
            body=note.body[:200],
            data={"inquiry_id": str(inquiry.id)},
        )
    elif not staff and inquiry.assigned_staff_id is not None:
        notify(
            db,
            user_id=inquiry.assigned_staff_id,
            type="inquiry_message",
            title=f"Customer replied on {inquiry.ref_no}",
            body=note.body[:200],
            data={"inquiry_id": str(inquiry.id)},
        )
    if staff:
        audit(
            db,
            actor_id=author.id,
            action="inquiry.note",
            entity="inquiry",
            entity_id=inquiry.id,
            meta={"internal": internal},
        )
    await db.commit()
    return note


# ---------------------------------------------------------------- consultation fee

FEE_OPEN = {"none", "requested", "rejected", "receipt_uploaded"}


def _require_consultation_admin(inquiry: Inquiry, user: User) -> None:
    if not (is_admin(user) and can_access_team(user, inquiry.visa_type_id)):
        raise Forbidden("Only this team's admin can do this")
    if inquiry.kind != "consultation":
        raise BadRequest("Only consultations have a fee", code="not_consultation")
    if inquiry.status in FINAL:
        raise BadRequest("This inquiry is closed", code="inquiry_closed")


async def _fee_email(inquiry: Inquiry, heading: str, message: str, reason: str | None = None):
    await job_queue.send_email(
        to=inquiry.customer.email,
        subject=f"{heading} · {inquiry.ref_no}",
        template="inquiry_update.html",
        context={
            "name": inquiry.customer.full_name,
            "heading": heading,
            "message": message,
            "reason": reason,
            "ref_no": inquiry.ref_no,
            "subject": inquiry.subject,
            "status": inquiry.status.value,
        },
    )


def _money(cents: int, currency: str) -> str:
    from app.services.pdf_service import money

    return money(cents, currency)


async def set_fee(db: AsyncSession, inquiry: Inquiry, admin: User, amount: str) -> None:
    """Team admin: the consultation fee the customer pays outside the app."""
    from app.services.case_service import parse_amount

    _require_consultation_admin(inquiry, admin)
    if inquiry.fee_status == "approved":
        raise BadRequest("The fee is already paid and approved", code="fee_approved")
    cents = parse_amount(amount)
    inquiry.fee_cents = cents
    inquiry.fee_currency = "AUD"
    if inquiry.fee_status in ("none", "rejected"):
        inquiry.fee_status = "requested"
    audit(
        db,
        actor_id=admin.id,
        action="inquiry.fee_set",
        entity="inquiry",
        entity_id=inquiry.id,
        meta={"amount_cents": cents},
    )
    fee = _money(cents, inquiry.fee_currency)
    notify(
        db,
        user_id=inquiry.customer_id,
        type="inquiry_fee",
        title=f"Consultation fee: {fee}",
        body=f"Pay {fee} by bank transfer and upload your receipt in {inquiry.ref_no}.",
        data={"inquiry_id": str(inquiry.id)},
    )
    await db.commit()
    await _fee_email(
        inquiry,
        "Your consultation fee",
        f"The fee for your consultation is {fee}. Please pay it by bank transfer (the bank details "
        "are in the app, on your inquiry) and upload the receipt there. We'll book your meeting "
        "once the payment is confirmed.",
    )


async def upload_fee_receipt(db: AsyncSession, inquiry: Inquiry, customer: User, file) -> None:
    """Customer: the receipt of the consultation fee paid outside the app."""
    import uuid as _uuid

    from app.core.config import settings
    from app.services.storage_service import ALLOWED_TYPES, get_storage, sniff_file_type

    if inquiry.customer_id != customer.id:
        raise Forbidden("Only the customer can upload the receipt")
    if inquiry.fee_status not in ("requested", "rejected", "receipt_uploaded"):
        raise BadRequest("There's no fee to pay for this inquiry yet", code="no_fee")
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise BadRequest(
            f"Files must be {settings.max_upload_bytes // (1024 * 1024)} MB or smaller",
            code="file_too_large",
            status_code=413,
        )
    mime = sniff_file_type(data) if data else None
    if mime is None:
        raise BadRequest(
            "Upload the receipt as a PDF, JPG or PNG", code="unsupported_file_type", status_code=415
        )
    key = f"inquiries/{inquiry.id}/receipt-{_uuid.uuid4().hex}{ALLOWED_TYPES[mime]}"
    await get_storage().save(key, data, mime)
    old_key = inquiry.fee_receipt_key
    inquiry.fee_receipt_key = key
    inquiry.fee_receipt_name = (file.filename or "receipt")[:255]
    inquiry.fee_receipt_mime = mime
    inquiry.fee_receipt_uploaded_at = utcnow()
    inquiry.fee_status = "receipt_uploaded"
    inquiry.fee_rejection_reason = None
    admins = await db.scalars(
        select(User.id).where(
            User.role == UserRole.ADMIN,
            User.team_id == inquiry.visa_type_id,
            User.is_active.is_(True),
        )
    )
    for admin_id in admins:
        notify(
            db,
            user_id=admin_id,
            type="inquiry_fee_receipt",
            title=f"Payment receipt to check: {inquiry.ref_no}",
            body=f"{inquiry.customer.full_name} uploaded the consultation fee receipt.",
            data={"inquiry_id": str(inquiry.id)},
        )
    await db.commit()
    if old_key and old_key != key:
        import contextlib

        with contextlib.suppress(Exception):
            await get_storage().delete(old_key)


async def fee_receipt_url(inquiry: Inquiry, user: User) -> str:
    from app.services.storage_service import get_storage

    if not inquiry.fee_receipt_key:
        raise NotFound("No receipt uploaded")
    return await get_storage().download_url(
        inquiry.fee_receipt_key,
        inquiry.fee_receipt_name or "receipt",
        inquiry.fee_receipt_mime or "application/octet-stream",
    )


async def review_fee(
    db: AsyncSession, inquiry: Inquiry, admin: User, approve: bool, reason: str | None
) -> None:
    """Team admin: the receipt is right (the meeting can now be booked), or ask for a new one."""
    _require_consultation_admin(inquiry, admin)
    if inquiry.fee_status != "receipt_uploaded":
        raise BadRequest("There's no receipt waiting to be checked", code="no_receipt")
    reason = (reason or "").strip() or None
    if not approve and not reason:
        raise BadRequest("Tell the customer what's wrong with the receipt", code="reason_required")
    fee = _money(inquiry.fee_cents or 0, inquiry.fee_currency)
    if approve:
        inquiry.fee_status = "approved"
        inquiry.fee_approved_at = utcnow()
        title, body = (
            "Payment confirmed",
            f"We received your {fee} consultation fee. We'll book your meeting soon.",
        )
    else:
        inquiry.fee_status = "rejected"
        inquiry.fee_rejection_reason = reason
        title, body = "Please upload your receipt again", reason
    audit(
        db,
        actor_id=admin.id,
        action="inquiry.fee_approved" if approve else "inquiry.fee_rejected",
        entity="inquiry",
        entity_id=inquiry.id,
        meta={"reason": reason} if reason else None,
    )
    notify(
        db,
        user_id=inquiry.customer_id,
        type="inquiry_fee",
        title=title,
        body=body,
        data={"inquiry_id": str(inquiry.id)},
    )
    await db.commit()
    if approve:
        await _fee_email(inquiry, "Payment confirmed", body)
    else:
        await _fee_email(
            inquiry,
            "Please upload your receipt again",
            "We couldn't confirm your consultation fee payment. Please upload the receipt again.",
            reason,
        )
