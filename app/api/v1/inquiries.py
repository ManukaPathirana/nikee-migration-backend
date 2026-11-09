import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import (
    AdminUser,
    CurrentUser,
    CustomerUser,
    DbSession,
    StaffOrAdmin,
    StaffUser,
)
from app.models import InquiryStatus
from app.schemas.common import Page, PageParams, page_params
from app.schemas.inquiry import (
    AssignIn,
    HistoryOut,
    InquiryCreate,
    InquiryDetail,
    InquiryOut,
    NoteIn,
    StatusChangeIn,
)
from app.services import inquiry_service as svc

router = APIRouter(prefix="/inquiries", tags=["inquiries"])


@router.get("", response_model=Page[InquiryOut])
async def list_inquiries(
    user: CurrentUser,
    db: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    scope: svc.Scope = "all",
    status_filter: Annotated[InquiryStatus | None, Query(alias="status")] = None,
    assigned_staff_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[InquiryOut]:
    """Customers: their own inquiries. Staff/admin: `scope=queue` (scheduled + unclaimed),
    `scope=mine` (assigned to me) or `all`, with optional status/staff/search filters."""
    rows, total = await svc.list_inquiries(
        db,
        user,
        params,
        scope=scope,
        status=status_filter,
        assigned_staff_id=assigned_staff_id,
        search=q,
    )
    return Page(
        items=[InquiryOut.model_validate(r) for r in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.post("", response_model=InquiryDetail, status_code=status.HTTP_201_CREATED)
async def create_inquiry(data: InquiryCreate, user: CustomerUser, db: DbSession) -> InquiryDetail:
    inquiry = await svc.create_inquiry(db, user, **data.model_dump())
    return svc.to_detail(inquiry, user)


@router.get("/{inquiry_id}", response_model=InquiryDetail)
async def get_inquiry(inquiry_id: uuid.UUID, user: CurrentUser, db: DbSession) -> InquiryDetail:
    return svc.to_detail(await svc.get_inquiry(db, inquiry_id, user), user)


@router.get("/{inquiry_id}/history", response_model=list[HistoryOut])
async def get_history(inquiry_id: uuid.UUID, user: CurrentUser, db: DbSession) -> list[HistoryOut]:
    inquiry = await svc.get_inquiry(db, inquiry_id, user)
    return [HistoryOut.model_validate(h) for h in inquiry.history]


@router.get("/{inquiry_id}/allowed-statuses", response_model=list[InquiryStatus])
async def allowed_statuses(
    inquiry_id: uuid.UUID, user: StaffOrAdmin, db: DbSession
) -> list[InquiryStatus]:
    inquiry = await svc.get_inquiry(db, inquiry_id, user)
    return sorted(svc.allowed_transitions(inquiry, user), key=list(InquiryStatus).index)


@router.post("/{inquiry_id}/claim", response_model=InquiryDetail)
async def claim(inquiry_id: uuid.UUID, user: StaffUser, db: DbSession) -> InquiryDetail:
    """Staff take an unclaimed scheduled inquiry. Admins use /assign instead."""
    inquiry = await svc.get_inquiry(db, inquiry_id, user)
    await svc.claim(db, inquiry, user)
    return svc.to_detail(await svc.get_inquiry(db, inquiry_id, user), user)


@router.post("/{inquiry_id}/assign", response_model=InquiryDetail)
async def assign(
    inquiry_id: uuid.UUID, data: AssignIn, user: AdminUser, db: DbSession
) -> InquiryDetail:
    inquiry = await svc.get_inquiry(db, inquiry_id, user)
    await svc.assign(db, inquiry, user, data.staff_id)
    return svc.to_detail(await svc.get_inquiry(db, inquiry_id, user), user)


@router.post("/{inquiry_id}/status", response_model=InquiryDetail)
async def change_status(
    inquiry_id: uuid.UUID, data: StatusChangeIn, user: StaffOrAdmin, db: DbSession
) -> InquiryDetail:
    inquiry = await svc.get_inquiry(db, inquiry_id, user)
    await svc.change_status(db, inquiry, user, data.status, data.reason)
    return svc.to_detail(await svc.get_inquiry(db, inquiry_id, user), user)


@router.post(
    "/{inquiry_id}/notes", response_model=InquiryDetail, status_code=status.HTTP_201_CREATED
)
async def add_note(
    inquiry_id: uuid.UUID, data: NoteIn, user: CurrentUser, db: DbSession
) -> InquiryDetail:
    """Staff/admin notes are internal by default; customer notes are always visible to staff."""
    inquiry = await svc.get_inquiry(db, inquiry_id, user)
    await svc.add_note(db, inquiry, user, body=data.body, is_internal=data.is_internal)
    return svc.to_detail(await svc.get_inquiry(db, inquiry_id, user), user)
