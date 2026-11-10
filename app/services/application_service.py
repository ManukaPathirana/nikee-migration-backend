"""Visa applications and their documents."""

import uuid
from pathlib import PurePath

from fastapi import UploadFile
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.config import settings
from app.core.errors import BadRequest, Forbidden, NotFound
from app.core.permissions import can_access_team, is_internal, team_scope
from app.core.security import utcnow
from app.models import (
    ApplicationDocument,
    ApplicationStatus,
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
    ChecklistItem,
    CustomerBrief,
    DocumentOut,
    Progress,
    RequirementOut,
    VisaTypeBrief,
)
from app.services import job_queue
from app.services.audit_service import audit
from app.services.notification_service import notify
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
            joinedload(VisaApplication.customer),
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


def _progress(app: VisaApplication) -> Progress:
    latest = _latest_by_requirement(app)
    mandatory = [r for r in app.visa_type.requirements if r.is_mandatory]
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
    checklist = [
        ChecklistItem(
            requirement=RequirementOut.model_validate(req),
            document=DocumentOut.model_validate(latest[req.id]) if req.id in latest else None,
        )
        for req in app.visa_type.requirements
    ]
    others = [DocumentOut.model_validate(d) for d in app.documents if d.requirement_id is None]
    return ApplicationDetail(
        **to_summary(app).model_dump(), checklist=checklist, other_documents=others
    )


# ---------------------------------------------------------------- queries


async def get_application(db: AsyncSession, app_id: uuid.UUID, user: User) -> VisaApplication:
    app = await db.scalar(base_query().where(VisaApplication.id == app_id))
    if app is None or (not _is_staff(user) and app.customer_id != user.id):
        raise NotFound("Application not found")
    if _is_staff(user) and not can_access_team(user, app.visa_type_id):
        raise NotFound("Application not found")  # another team's application
    return app


async def list_applications(
    db: AsyncSession,
    user: User,
    params: PageParams,
    *,
    status: ApplicationStatus | None = None,
    assigned_to_me: bool = False,
    team_id: uuid.UUID | None = None,
) -> tuple[list[VisaApplication], int]:
    query = base_query()
    if _is_staff(user):
        scope_team = team_scope(user) or team_id
        if scope_team is not None:
            query = query.where(VisaApplication.visa_type_id == scope_team)
    if not _is_staff(user):
        query = query.where(VisaApplication.customer_id == user.id)
    elif assigned_to_me:
        query = query.where(VisaApplication.assigned_staff_id == user.id)
    if status is not None:
        query = query.where(VisaApplication.status == status)
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = await db.scalars(
        query.order_by(VisaApplication.updated_at.desc())
        .offset(params.offset)
        .limit(params.page_size)
    )
    return list(rows), total or 0


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
    )
    db.add(app)
    await db.commit()
    return await get_application(db, app.id, user)


def _require_owner_editable(app: VisaApplication, user: User) -> None:
    if app.customer_id != user.id:
        raise Forbidden("Only the applicant can change this application")
    if app.status not in EDITABLE_STATUSES:
        raise BadRequest(
            "This application is closed and can no longer be changed", code="application_closed"
        )


async def submit_application(db: AsyncSession, app: VisaApplication, user: User) -> None:
    _require_owner_editable(app, user)
    if app.status != ApplicationStatus.DRAFT:
        raise BadRequest("This application has already been submitted", code="invalid_status")
    progress = _progress(app)
    if progress.uploaded < progress.required:
        missing = progress.required - progress.uploaded
        raise BadRequest(
            f"Upload all required documents first ({missing} missing)",
            code="documents_missing",
        )
    app.status = ApplicationStatus.SUBMITTED
    app.submitted_at = utcnow()
    await db.commit()


async def withdraw_application(db: AsyncSession, app: VisaApplication, user: User) -> None:
    if app.customer_id != user.id:
        raise Forbidden("Only the applicant can withdraw this application")
    if app.status not in WITHDRAWABLE:
        raise BadRequest("This application can no longer be withdrawn", code="invalid_status")
    app.status = ApplicationStatus.WITHDRAWN
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

    if requirement_id is not None:
        requirement = await db.get(DocumentRequirement, requirement_id)
        if requirement is None or requirement.visa_type_id != app.visa_type_id:
            raise BadRequest(
                "This document is not part of the checklist", code="invalid_requirement"
            )
        current = _latest_by_requirement(app).get(requirement_id)
        if current is not None and current.status == DocumentStatus.APPROVED:
            raise BadRequest("This document is already approved", code="already_approved")

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
    if app.status not in EDITABLE_STATUSES:
        raise BadRequest("This application is closed", code="application_closed")
    doc.status = status
    doc.rejection_reason = rejection_reason.strip() if status == DocumentStatus.REJECTED else None
    doc.reviewed_by = reviewer.id
    doc.reviewed_at = utcnow()
    if app.status == ApplicationStatus.SUBMITTED:
        app.status = ApplicationStatus.IN_REVIEW
    if app.assigned_staff_id is None and reviewer.role == UserRole.STAFF:
        app.assigned_staff_id = reviewer.id

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
    allowed = STAFF_TRANSITIONS.get(app.status, set())
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
    if app.assigned_staff_id is None and actor.role == UserRole.STAFF:
        app.assigned_staff_id = actor.id
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
