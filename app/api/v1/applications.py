import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.core.config import settings
from app.core.deps import CurrentUser, CustomerUser, DbSession, StaffOrAdmin
from app.models import ApplicationStatus
from app.schemas.common import Page, PageParams, page_params
from app.schemas.visa import (
    ApplicationCreate,
    ApplicationDetail,
    ApplicationStatusIn,
    ApplicationSummary,
    DocumentOut,
    DocumentReviewIn,
    DownloadUrlOut,
)
from app.services import application_service as svc
from app.services.storage_service import get_storage

router = APIRouter(tags=["applications"])


@router.get("/applications", response_model=Page[ApplicationSummary])
async def list_applications(
    user: CurrentUser,
    db: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[ApplicationStatus | None, Query(alias="status")] = None,
    assigned_to_me: bool = False,
) -> Page[ApplicationSummary]:
    """Customers see their own applications; staff and admin see all."""
    rows, total = await svc.list_applications(
        db, user, params, status=status_filter, assigned_to_me=assigned_to_me
    )
    return Page(
        items=[svc.to_summary(a) for a in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.post("/applications", response_model=ApplicationDetail, status_code=status.HTTP_201_CREATED)
async def create_application(
    data: ApplicationCreate, user: CustomerUser, db: DbSession
) -> ApplicationDetail:
    app = await svc.create_application(
        db, user, visa_type_id=data.visa_type_id, destination_country=data.destination_country
    )
    return svc.to_detail(app)


@router.get("/applications/{app_id}", response_model=ApplicationDetail)
async def get_application(app_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ApplicationDetail:
    return svc.to_detail(await svc.get_application(db, app_id, user))


@router.post("/applications/{app_id}/submit", response_model=ApplicationDetail)
async def submit_application(
    app_id: uuid.UUID, user: CustomerUser, db: DbSession
) -> ApplicationDetail:
    app = await svc.get_application(db, app_id, user)
    await svc.submit_application(db, app, user)
    return svc.to_detail(await svc.get_application(db, app_id, user))


@router.post("/applications/{app_id}/withdraw", response_model=ApplicationDetail)
async def withdraw_application(
    app_id: uuid.UUID, user: CustomerUser, db: DbSession
) -> ApplicationDetail:
    app = await svc.get_application(db, app_id, user)
    await svc.withdraw_application(db, app, user)
    return svc.to_detail(await svc.get_application(db, app_id, user))


@router.post("/applications/{app_id}/status", response_model=ApplicationDetail)
async def change_application_status(
    app_id: uuid.UUID, data: ApplicationStatusIn, user: StaffOrAdmin, db: DbSession
) -> ApplicationDetail:
    app = await svc.get_application(db, app_id, user)
    await svc.change_status(db, app, user, data.status)
    return svc.to_detail(await svc.get_application(db, app_id, user))


# ---------------------------------------------------------------- documents


@router.post(
    "/applications/{app_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    app_id: uuid.UUID,
    user: CustomerUser,
    db: DbSession,
    file: Annotated[UploadFile, File(description="PDF, JPG or PNG, max 10 MB")],
    requirement_id: Annotated[uuid.UUID | None, Form()] = None,
) -> DocumentOut:
    app = await svc.get_application(db, app_id, user)
    doc = await svc.upload_document(db, app, user, file=file, requirement_id=requirement_id)
    return DocumentOut.model_validate(doc)


@router.get("/documents/{doc_id}/download-url", response_model=DownloadUrlOut)
async def document_download_url(
    doc_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> DownloadUrlOut:
    """Short-lived link to view the file. Relative URLs are relative to the API origin."""
    doc, _ = await svc.get_document(db, doc_id, user)
    url = await get_storage().download_url(doc.file_key, doc.file_name, doc.mime_type)
    return DownloadUrlOut(url=url, expires_in=settings.signed_url_expire_seconds)


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(doc_id: uuid.UUID, user: CustomerUser, db: DbSession) -> Response:
    doc, app = await svc.get_document(db, doc_id, user)
    await svc.delete_document(db, doc, app, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/documents/{doc_id}/review", response_model=DocumentOut)
async def review_document(
    doc_id: uuid.UUID, data: DocumentReviewIn, user: StaffOrAdmin, db: DbSession
) -> DocumentOut:
    doc, app = await svc.get_document(db, doc_id, user)
    await svc.review_document(
        db, doc, app, user, status=data.status, rejection_reason=data.rejection_reason
    )
    return DocumentOut.model_validate(doc)
