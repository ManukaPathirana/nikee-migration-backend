"""Visa cases (applications) and their documents.

Flow: intake (passport + visa grant) -> team admin assigns a staff member -> contract and deposit
-> full document checklist unlocks -> final invoice -> data confirmation -> completed.
The step-by-step actions live in case_service; this module holds reads, uploads and reviews.
"""

import uuid
from pathlib import PurePath

from fastapi import UploadFile
from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.config import settings
from app.core.errors import BadRequest, Forbidden, NotFound
from app.core.permissions import can_access_team, is_admin, is_internal, team_scope
from app.core.security import utcnow
from app.models import (
    ApplicationDocument,
    ApplicationStatus,
    CaseStage,
    DocumentRequirement,
    DocumentStatus,
    User,
    UserRole,
    VisaApplication,
    VisaType,
)
from app.schemas.common import PageParams
from app.schemas.visa import (
    ApplicationDetail,
    ApplicationSummary,
    CaseFileOut,
    ChecklistItem,
    CustomerBrief,
    DocumentOut,
    InvoiceOut,
    Progress,
    RequirementOut,
    StaffBriefOut,
    VisaTypeBrief,
)
from app.services import job_queue
from app.services.audit_service import audit
from app.services.notification_service import notify, notify_super_admins
from app.services.storage_service import ALLOWED_TYPES, get_storage, sniff_file_type

# Customers may add/replace documents only while the application is open.
EDITABLE_STATUSES = {
    ApplicationStatus.DRAFT,
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.IN_REVIEW,
}
STAFF_TRANSITIONS: dict[ApplicationStatus, set[ApplicationStatus]] = {
    ApplicationStatus.SUBMITTED: {ApplicationStatus.IN_REVIEW, ApplicationStatus.REJECTED},
    ApplicationStatus.IN_REVIEW: {ApplicationStatus.APPROVED, ApplicationStatus.REJECTED},
}
WITHDRAWABLE = EDITABLE_STATUSES

# Who acts next at each stage.
WAITING_ON: dict[CaseStage, str] = {
    CaseStage.INTAKE: "customer",
    CaseStage.ASSIGNMENT: "admin",
    CaseStage.CONTRACT_PENDING: "staff",
    CaseStage.AGREEMENT: "customer",
    CaseStage.AGREEMENT_REVIEW: "staff",
    CaseStage.DOCUMENTS: "customer",  # "staff" when uploads are waiting for review
    CaseStage.FINAL_PAYMENT: "customer",
    CaseStage.CONFIRMATION_PENDING: "staff",
    CaseStage.CONFIRMATION: "customer",
    CaseStage.CONFIRMATION_REVIEW: "staff",
    CaseStage.COMPLETED: "none",
    CaseStage.WITHDRAWN: "none",
}
# The full checklist is open from the documents stage onwards.
UNLOCKED_STAGES = {
    CaseStage.DOCUMENTS,
    CaseStage.FINAL_PAYMENT,
    CaseStage.CONFIRMATION_PENDING,
    CaseStage.CONFIRMATION,
    CaseStage.CONFIRMATION_REVIEW,
    CaseStage.COMPLETED,
}
# Customers can still add/replace documents in these stages.
UPLOAD_STAGES = {
    CaseStage.INTAKE,
    CaseStage.ASSIGNMENT,
    CaseStage.CONTRACT_PENDING,
    CaseStage.AGREEMENT,
    CaseStage.AGREEMENT_REVIEW,
    CaseStage.DOCUMENTS,
}


def stage_of(app: VisaApplication) -> CaseStage:
    return CaseStage(app.stage)


def documents_unlocked(app: VisaApplication) -> bool:
    return stage_of(app) in UNLOCKED_STAGES


def has_pending_uploads(app: VisaApplication) -> bool:
    return any(d.status == DocumentStatus.PENDING for d in app.documents)


def waiting_on(app: VisaApplication) -> str:
    stage = stage_of(app)
    if stage == CaseStage.DOCUMENTS and has_pending_uploads(app):
        return "staff"
    return WAITING_ON[stage]


def set_stage(app: VisaApplication, stage: CaseStage) -> None:
    """Moves the case and keeps the older status field in step."""
    app.stage = stage.value
    app.stage_changed_at = utcnow()
    if stage == CaseStage.INTAKE:
        app.status = ApplicationStatus.DRAFT
    elif stage == CaseStage.COMPLETED:
        app.status = ApplicationStatus.APPROVED
    elif stage == CaseStage.WITHDRAWN:
        app.status = ApplicationStatus.WITHDRAWN
    elif stage in UNLOCKED_STAGES:
        app.status = ApplicationStatus.IN_REVIEW
    else:
        app.status = ApplicationStatus.SUBMITTED
        if app.submitted_at is None:
            app.submitted_at = utcnow()


def _is_staff(user: User) -> bool:
    """Staff, team admin or super admin."""
    return is_internal(user)


def base_query() -> Select[tuple[VisaApplication]]:
    # populate_existing: always reload collections, even for objects already in the session.
    return (
        select(VisaApplication)
        .options(
            joinedload(VisaApplication.visa_type).selectinload(VisaType.requirements),
            selectinload(VisaApplication.documents),
            selectinload(VisaApplication.files),
            selectinload(VisaApplication.invoices),
            selectinload(VisaApplication.extra_requirements),
            joinedload(VisaApplication.customer),
            joinedload(VisaApplication.assigned_staff),
        )
        .execution_options(populate_existing=True)
    )


# ---------------------------------------------------------------- read models


def _latest_by_requirement(app: VisaApplication) -> dict[uuid.UUID, ApplicationDocument]:
    latest: dict[uuid.UUID, ApplicationDocument] = {}
    for doc in app.documents:  # ordered by created_at
        if doc.requirement_id is not None:
            latest[doc.requirement_id] = doc
    return latest


def all_requirements(app: VisaApplication) -> list:
    """The visa type's checklist plus extra documents requested for this case."""
    return [*app.visa_type.requirements, *app.extra_requirements]


def visible_requirements(app: VisaApplication) -> list:
    """Customers see only the documents to start with (passport, visa grant) until their signed
    contract and deposit are approved; then the whole checklist."""
    from app.core.viewer import viewer_is_customer

    reqs = all_requirements(app)
    if viewer_is_customer() and not documents_unlocked(app):
        reqs = [r for r in reqs if r.is_intake]
    return reqs


def _progress(app: VisaApplication) -> Progress:
    latest = _latest_by_requirement(app)
    mandatory = [r for r in visible_requirements(app) if r.is_mandatory]
    uploaded = approved = 0
    for req in mandatory:
        doc = latest.get(req.id)
        if doc and doc.status != DocumentStatus.REJECTED:
            uploaded += 1
        if doc and doc.status == DocumentStatus.APPROVED:
            approved += 1
    return Progress(required=len(mandatory), uploaded=uploaded, approved=approved)


def to_summary(app: VisaApplication) -> ApplicationSummary:
    return ApplicationSummary(
        id=app.id,
        ref_no=app.ref_no,
        stage=stage_of(app),
        waiting_on=waiting_on(app),
        stage_changed_at=app.stage_changed_at,
        assigned_staff=(
            StaffBriefOut.model_validate(app.assigned_staff) if app.assigned_staff else None
        ),
        visa_type=VisaTypeBrief.model_validate(app.visa_type),
        destination_country=app.destination_country,
        status=app.status,
        customer=CustomerBrief.model_validate(app.customer),
        assigned_staff_id=app.assigned_staff_id,
        submitted_at=app.submitted_at,
        created_at=app.created_at,
        updated_at=app.updated_at,
        progress=_progress(app),
    )


def to_detail(app: VisaApplication) -> ApplicationDetail:
    latest = _latest_by_requirement(app)
    unlocked = documents_unlocked(app)
    checklist = [
        ChecklistItem(
            requirement=RequirementOut.model_validate(req),
            document=DocumentOut.model_validate(latest[req.id]) if req.id in latest else None,
            locked=not (unlocked or req.is_intake),
        )
        for req in visible_requirements(app)
    ]
    others = [DocumentOut.model_validate(d) for d in app.documents if d.requirement_id is None]
    return ApplicationDetail(
        **to_summary(app).model_dump(),
        checklist=checklist,
        other_documents=others,
        documents_unlocked=unlocked,
        files=[CaseFileOut.model_validate(f) for f in app.files],
        invoices=[InvoiceOut.model_validate(i) for i in app.invoices],
    )


# ---------------------------------------------------------------- queries


async def get_application(db: AsyncSession, app_id: uuid.UUID, user: User) -> VisaApplication:
    app = await db.scalar(base_query().where(VisaApplication.id == app_id))
    if app is None or (not _is_staff(user) and app.customer_id != user.id):
        raise NotFound("Application not found")
    if _is_staff(user) and not can_access_team(user, app.visa_type_id):
        raise NotFound("Application not found")  # another team's application
    # A case belongs to one staff member; other staff in the team don't see it.
    if user.role == UserRole.STAFF and app.assigned_staff_id != user.id:
        raise NotFound("Application not found")
    return app


async def list_applications(
    db: AsyncSession,
    user: User,
    params: PageParams,
    *,
    status: ApplicationStatus | None = None,
    assigned_to_me: bool = False,
    team_id: uuid.UUID | None = None,
    stage: CaseStage | None = None,
    waiting: str | None = None,
) -> tuple[list[VisaApplication], int]:
    query = base_query()
    if _is_staff(user):
        scope_team = team_scope(user) or team_id
        if scope_team is not None:
            query = query.where(VisaApplication.visa_type_id == scope_team)
    if not _is_staff(user):
        query = query.where(VisaApplication.customer_id == user.id)
    elif assigned_to_me or user.role == UserRole.STAFF:
        query = query.where(VisaApplication.assigned_staff_id == user.id)
    if status is not None:
        query = query.where(VisaApplication.status == status)
    if stage is not None:
        query = query.where(VisaApplication.stage == stage.value)
    if waiting is not None:
        stages = [s.value for s, who in WAITING_ON.items() if who == waiting]
        if waiting in ("customer", "staff"):
            stages.append(CaseStage.DOCUMENTS.value)  # either side, refined below
        query = query.where(VisaApplication.stage.in_(stages))
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = list(
        await db.scalars(
            query.order_by(VisaApplication.updated_at.desc())
            .offset(params.offset)
            .limit(params.page_size)
        )
    )
    if waiting in ("customer", "staff"):
        rows = [a for a in rows if waiting_on(a) == waiting]
    return rows, total or 0


# ---------------------------------------------------------------- customer actions


async def create_application(
    db: AsyncSession, user: User, *, visa_type_id: uuid.UUID, destination_country: str
) -> VisaApplication:
    visa = await db.get(VisaType, visa_type_id)
    if visa is None or not visa.is_active:
        raise NotFound("Visa type not found")
    app = VisaApplication(
        customer_id=user.id,
        visa_type_id=visa.id,
        destination_country=destination_country.strip(),
        ref_no=await next_case_ref(db),
        stage=CaseStage.INTAKE.value,
        stage_changed_at=utcnow(),
    )
    db.add(app)
    await db.commit()
    return await get_application(db, app.id, user)


async def next_case_ref(db: AsyncSession) -> str:
    """NM-C-<year>-<NNNN>, sequential per year (advisory lock serialises creates)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('case_ref_no'))"))
    prefix = f"NM-C-{utcnow().year}-"
    last = await db.scalar(
        select(VisaApplication.ref_no)
        .where(VisaApplication.ref_no.like(f"{prefix}%"))
        .order_by(func.length(VisaApplication.ref_no).desc(), VisaApplication.ref_no.desc())
        .limit(1)
    )
    number = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{number:04d}"


def _require_owner_editable(app: VisaApplication, user: User) -> None:
    if app.customer_id != user.id:
        raise Forbidden("Only the applicant can change this application")
    if stage_of(app) not in UPLOAD_STAGES:
        raise BadRequest(
            "Documents can't be changed at this step of your case", code="application_closed"
        )


async def submit_application(db: AsyncSession, app: VisaApplication, user: User) -> None:
    """Intake: passport + visa grant are in; the case goes to the visa type's team admin."""
    _require_owner_editable(app, user)
    if stage_of(app) != CaseStage.INTAKE:
        raise BadRequest("This case has already been submitted", code="invalid_status")
    latest = _latest_by_requirement(app)
    missing = [
        r.name
        for r in app.visa_type.requirements
        if r.is_intake
        and r.is_mandatory
        and (r.id not in latest or latest[r.id].status == DocumentStatus.REJECTED)
    ]
    if missing:
        raise BadRequest(
            f"Upload these first: {', '.join(missing)}",
            code="documents_missing",
        )
    set_stage(app, CaseStage.ASSIGNMENT)
    admins = await db.scalars(
        select(User.id).where(
            User.role == UserRole.ADMIN,
            User.team_id == app.visa_type_id,
            User.is_active.is_(True),
        )
    )
    for admin_id in admins:
        notify(
            db,
            user_id=admin_id,
            type="case_new",
            title=f"New {app.visa_type.name} case {app.ref_no}",
            body=f"{app.customer.full_name} is ready. Assign a staff member.",
            data={"application_id": str(app.id)},
        )
    await notify_super_admins(
        db,
        type="case_new",
        title=f"New {app.visa_type.name} case {app.ref_no}",
        body=f"{app.customer.full_name} sent their documents. The team admin assigns it.",
        data={"application_id": str(app.id)},
    )
    await db.commit()


async def withdraw_application(db: AsyncSession, app: VisaApplication, user: User) -> None:
    if app.customer_id != user.id:
        raise Forbidden("Only the applicant can withdraw this application")
    if stage_of(app) in (CaseStage.COMPLETED, CaseStage.WITHDRAWN):
        raise BadRequest("This application can no longer be withdrawn", code="invalid_status")
    set_stage(app, CaseStage.WITHDRAWN)
    if app.assigned_staff_id is not None:
        notify(
            db,
            user_id=app.assigned_staff_id,
            type="case_update",
            title=f"{app.ref_no or 'A case'} was withdrawn",
            body=f"{app.customer.full_name} withdrew their {app.visa_type.name} application.",
            data={"application_id": str(app.id), "stage": app.stage},
        )
    await notify_super_admins(
        db,
        type="case_update",
        title=f"{app.ref_no or 'A case'} was withdrawn",
        body=f"{app.customer.full_name} withdrew their {app.visa_type.name} application.",
        data={"application_id": str(app.id), "stage": app.stage},
    )
    await db.commit()


async def upload_document(
    db: AsyncSession,
    app: VisaApplication,
    user: User,
    *,
    file: UploadFile,
    requirement_id: uuid.UUID | None,
) -> ApplicationDocument:
    _require_owner_editable(app, user)

    requirement = None
    if requirement_id is not None:
        requirement = await db.get(DocumentRequirement, requirement_id)
        if (
            requirement is None
            or requirement.visa_type_id != app.visa_type_id
            or requirement.application_id not in (None, app.id)
        ):
            raise BadRequest(
                "This document is not part of the checklist", code="invalid_requirement"
            )
        current = _latest_by_requirement(app).get(requirement_id)
        if current is not None and current.status == DocumentStatus.APPROVED:
            raise BadRequest("This document is already approved", code="already_approved")
        if not requirement.is_intake and not documents_unlocked(app):
            raise Forbidden(
                "This document unlocks after your signed contract and deposit are approved",
                code="documents_locked",
            )
    elif not documents_unlocked(app):
        raise Forbidden(
            "Extra documents unlock after your signed contract and deposit are approved",
            code="documents_locked",
        )

    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise BadRequest("Files must be 10 MB or smaller", code="file_too_large", status_code=413)
    if not data:
        raise BadRequest("The file is empty", code="empty_file")
    mime = sniff_file_type(data)
    if mime is None:
        raise BadRequest(
            "Only PDF, JPG and PNG files are accepted",
            code="unsupported_file_type",
            status_code=415,
        )

    original = PurePath(file.filename or "document").name[:200] or "document"
    key = f"applications/{app.id}/{uuid.uuid4().hex}{ALLOWED_TYPES[mime]}"
    await get_storage().save(key, data, mime)

    doc = ApplicationDocument(
        application_id=app.id,
        requirement_id=requirement_id,
        file_key=key,
        file_name=original,
        mime_type=mime,
        size_bytes=len(data),
    )
    db.add(doc)
    # Once the case is with its staff member, tell them there is something to review.
    if app.assigned_staff_id is not None and stage_of(app) == CaseStage.DOCUMENTS:
        what = requirement.name if requirement is not None else "an extra document"
        notify(
            db,
            user_id=app.assigned_staff_id,
            type="document_uploaded",
            title=f"{app.ref_no}: new document to review",
            body=f"{app.customer.full_name} uploaded {what}.",
            data={"application_id": str(app.id)},
        )
    await db.commit()
    return doc


async def get_document(
    db: AsyncSession, doc_id: uuid.UUID, user: User
) -> tuple[ApplicationDocument, VisaApplication]:
    doc = await db.get(ApplicationDocument, doc_id)
    if doc is None:
        raise NotFound("Document not found")
    app = await get_application(db, doc.application_id, user)  # enforces access
    return doc, app


async def delete_document(
    db: AsyncSession, doc: ApplicationDocument, app: VisaApplication, user: User
) -> None:
    _require_owner_editable(app, user)
    if doc.status != DocumentStatus.PENDING:
        raise BadRequest("Only documents waiting for review can be removed", code="invalid_status")
    await db.delete(doc)
    await db.commit()
    await get_storage().delete(doc.file_key)


# ---------------------------------------------------------------- staff actions


async def review_document(
    db: AsyncSession,
    doc: ApplicationDocument,
    app: VisaApplication,
    reviewer: User,
    *,
    status: DocumentStatus,
    rejection_reason: str | None,
) -> None:
    if stage_of(app) in (CaseStage.COMPLETED, CaseStage.WITHDRAWN, CaseStage.INTAKE):
        raise BadRequest("Documents can't be reviewed at this step", code="application_closed")
    if reviewer.role == UserRole.STAFF and app.assigned_staff_id != reviewer.id:
        raise Forbidden("Only the case's staff member can review its documents")
    doc.status = status
    doc.rejection_reason = rejection_reason.strip() if status == DocumentStatus.REJECTED else None
    doc.reviewed_by = reviewer.id
    doc.reviewed_at = utcnow()

    approved = status == DocumentStatus.APPROVED
    title = f"Document {'approved' if approved else 'needs attention'}: {doc.file_name}"
    body = (
        "Your document was approved."
        if approved
        else f"Your document was rejected: {doc.rejection_reason}. Please upload a new copy."
    )
    notify(
        db,
        user_id=app.customer_id,
        type="document_reviewed",
        title=title,
        body=body,
        data={"application_id": str(app.id), "document_id": str(doc.id), "status": status.value},
    )
    audit(
        db,
        actor_id=reviewer.id,
        action=f"document.{status.value}",
        entity="application_document",
        entity_id=doc.id,
        meta={"application_id": str(app.id), "reason": doc.rejection_reason},
    )
    await db.commit()
    await job_queue.send_email(
        to=app.customer.email,
        subject=title,
        template="document_reviewed.html",
        context={
            "name": app.customer.full_name,
            "file_name": doc.file_name,
            "visa_type": app.visa_type.name,
            "approved": approved,
            "reason": doc.rejection_reason,
        },
    )


async def change_status(
    db: AsyncSession, app: VisaApplication, actor: User, new_status: ApplicationStatus
) -> None:
    # The case steps (contract, payments, confirmation) now move applications forward;
    # this endpoint only remains for rejecting a case.
    if new_status != ApplicationStatus.REJECTED:
        raise BadRequest(
            "Use the case steps to move this application forward", code="use_case_steps"
        )
    if not is_admin(actor) and app.assigned_staff_id != actor.id:
        raise Forbidden("Only the case's staff member or a team admin can reject it")
    allowed = STAFF_TRANSITIONS.get(app.status, set()) | {ApplicationStatus.REJECTED}
    if stage_of(app) in (CaseStage.COMPLETED, CaseStage.WITHDRAWN):
        allowed = set()
    if new_status not in allowed:
        raise BadRequest(
            f"Can't change an application from {app.status.value} to {new_status.value}",
            code="invalid_transition",
        )
    if new_status == ApplicationStatus.APPROVED:
        progress = _progress(app)
        if progress.approved < progress.required:
            raise BadRequest(
                "Approve all required documents before approving the application",
                code="documents_not_approved",
            )
    old = app.status
    app.status = new_status
    app.stage = CaseStage.WITHDRAWN.value
    app.stage_changed_at = utcnow()
    notify(
        db,
        user_id=app.customer_id,
        type="application_status",
        title=f"Application {new_status.value.replace('_', ' ')}",
        body=f"Your {app.visa_type.name} application is now {new_status.value.replace('_', ' ')}.",
        data={"application_id": str(app.id), "status": new_status.value},
    )
    audit(
        db,
        actor_id=actor.id,
        action="application.status",
        entity="visa_application",
        entity_id=app.id,
        meta={"from": old.value, "to": new_status.value},
    )
    await db.commit()
