import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.v1.company import get_company_row
from app.core.deps import AdminUser, DbSession
from app.models import CompanyInfo
from app.schemas.admin import (
    ActiveIn,
    CustomerDetail,
    CustomerOut,
    DashboardOut,
    RequirementCreate,
    RequirementUpdate,
    StaffCreate,
    StaffOut,
    StaffUpdate,
    VisaTypeAdminOut,
    VisaTypeCreate,
    VisaTypeUpdate,
)
from app.schemas.common import MessageOut, Page, PageParams, page_params
from app.schemas.company import CompanyOut, CompanyUpdate
from app.schemas.inquiry import BoardOut, InquiryOut
from app.schemas.review import ReviewModerate, ReviewOut
from app.services import admin_service, inquiry_service, review_service
from app.services.audit_service import audit

router = APIRouter(prefix="/admin", tags=["admin"])

Params = Annotated[PageParams, Depends(page_params)]
Search = Annotated[str | None, Query(max_length=100)]


# ---------------------------------------------------------------- company


@router.put("/company", response_model=CompanyOut)
async def update_company(data: CompanyUpdate, user: AdminUser, db: DbSession) -> CompanyInfo:
    company = await get_company_row(db)
    changes = data.model_dump(exclude_unset=True, mode="json")
    for field, value in changes.items():
        if field in {"phone", "email", "whatsapp", "address", "about"} and value == "":
            value = None
        if field == "website_url" and value is not None:
            value = value.rstrip("/")
        setattr(company, field, value)
    audit(
        db,
        actor_id=user.id,
        action="company.update",
        entity="company_info",
        entity_id=company.id,
        meta={"fields": sorted(changes)},
    )
    await db.commit()
    return company


# ---------------------------------------------------------------- dashboard + board


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(_: AdminUser, db: DbSession) -> DashboardOut:
    return await admin_service.dashboard(db)


@router.get("/inquiries/board", response_model=BoardOut)
async def inquiry_board(_: AdminUser, db: DbSession) -> BoardOut:
    """Kanban: latest 50 inquiries per status column, plus total counts."""
    columns = await inquiry_service.board(db)
    return BoardOut(
        columns={s: [InquiryOut.model_validate(i) for i in rows] for s, rows in columns.items()},
        counts=await inquiry_service.board_counts(db),
    )


# ---------------------------------------------------------------- staff


@router.get("/staff", response_model=Page[StaffOut])
async def list_staff(
    _: AdminUser, db: DbSession, params: Params, q: Search = None, active: bool | None = None
) -> Page[StaffOut]:
    items, total = await admin_service.list_staff(db, params, q=q, active=active)
    return Page(items=items, total=total, page=params.page, page_size=params.page_size)


@router.post("/staff", response_model=StaffOut, status_code=status.HTTP_201_CREATED)
async def create_staff(data: StaffCreate, admin: AdminUser, db: DbSession) -> StaffOut:
    """Creates the account and emails an invite link to set a password."""
    user = await admin_service.create_staff(db, admin, **data.model_dump())
    return await admin_service.staff_out(db, await admin_service.get_staff(db, user.id))


@router.patch("/staff/{staff_id}", response_model=StaffOut)
async def update_staff(
    staff_id: uuid.UUID, data: StaffUpdate, admin: AdminUser, db: DbSession
) -> StaffOut:
    user = await admin_service.get_staff(db, staff_id)
    await admin_service.update_staff(db, admin, user, data.model_dump(exclude_unset=True))
    return await admin_service.staff_out(db, user)


@router.post("/staff/{staff_id}/active", response_model=StaffOut)
async def set_staff_active(
    staff_id: uuid.UUID, data: ActiveIn, admin: AdminUser, db: DbSession
) -> StaffOut:
    """Deactivating signs the person out everywhere. Their open inquiries stay assigned."""
    user = await admin_service.get_staff(db, staff_id)
    await admin_service.set_active(db, admin, user, data.is_active)
    return await admin_service.staff_out(db, user)


@router.post("/staff/{staff_id}/resend-invite", response_model=MessageOut)
async def resend_invite(staff_id: uuid.UUID, admin: AdminUser, db: DbSession) -> MessageOut:
    user = await admin_service.get_staff(db, staff_id)
    await admin_service.resend_invite(db, admin, user)
    return MessageOut(message=f"Invite sent to {user.email}")


# ---------------------------------------------------------------- customers


@router.get("/customers", response_model=Page[CustomerOut])
async def list_customers(
    _: AdminUser, db: DbSession, params: Params, q: Search = None, active: bool | None = None
) -> Page[CustomerOut]:
    items, total = await admin_service.list_customers(db, params, q=q, active=active)
    return Page(items=items, total=total, page=params.page, page_size=params.page_size)


@router.get("/customers/{customer_id}", response_model=CustomerDetail)
async def get_customer(customer_id: uuid.UUID, _: AdminUser, db: DbSession) -> CustomerDetail:
    user = await admin_service.get_customer(db, customer_id)
    return await admin_service.customer_detail(db, user)


@router.post("/customers/{customer_id}/active", response_model=CustomerDetail)
async def set_customer_active(
    customer_id: uuid.UUID, data: ActiveIn, admin: AdminUser, db: DbSession
) -> CustomerDetail:
    user = await admin_service.get_customer(db, customer_id)
    await admin_service.set_active(db, admin, user, data.is_active)
    return await admin_service.customer_detail(db, user)


# ---------------------------------------------------------------- reviews


@router.get("/reviews", response_model=Page[ReviewOut])
async def list_reviews(
    admin: AdminUser,
    db: DbSession,
    params: Params,
    rating: Annotated[int | None, Query(ge=1, le=5)] = None,
    visible: bool | None = None,
    flagged: bool | None = None,
    staff_id: uuid.UUID | None = None,
) -> Page[ReviewOut]:
    rows, total = await review_service.list_reviews(
        db, admin, params, rating=rating, visible=visible, flagged=flagged, staff_id=staff_id
    )
    return Page(
        items=[ReviewOut.model_validate(r) for r in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.patch("/reviews/{review_id}", response_model=ReviewOut)
async def moderate_review(
    review_id: uuid.UUID, data: ReviewModerate, admin: AdminUser, db: DbSession
) -> ReviewOut:
    """Hide/show, flag/unflag, or reply (the customer is notified of replies)."""
    review = await review_service.get_review(db, review_id)
    updated = await review_service.moderate(db, admin, review, data.model_dump(exclude_unset=True))
    return ReviewOut.model_validate(updated)


# ---------------------------------------------------------------- visa types


@router.get("/visa-types", response_model=list[VisaTypeAdminOut])
async def list_visa_types(_: AdminUser, db: DbSession) -> list:
    """All visa types, including inactive ones."""
    return await admin_service.list_visa_types(db)


@router.post("/visa-types", response_model=VisaTypeAdminOut, status_code=status.HTTP_201_CREATED)
async def create_visa_type(data: VisaTypeCreate, admin: AdminUser, db: DbSession):
    return await admin_service.create_visa_type(db, admin, data.model_dump())


@router.patch("/visa-types/{visa_id}", response_model=VisaTypeAdminOut)
async def update_visa_type(
    visa_id: uuid.UUID, data: VisaTypeUpdate, admin: AdminUser, db: DbSession
):
    visa = await admin_service.get_visa_type(db, visa_id)
    return await admin_service.update_visa_type(
        db, admin, visa, data.model_dump(exclude_unset=True)
    )


@router.post(
    "/visa-types/{visa_id}/requirements",
    response_model=VisaTypeAdminOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_requirement(
    visa_id: uuid.UUID, data: RequirementCreate, admin: AdminUser, db: DbSession
):
    visa = await admin_service.get_visa_type(db, visa_id)
    return await admin_service.add_requirement(db, admin, visa, data.model_dump())


@router.patch("/requirements/{req_id}", response_model=VisaTypeAdminOut)
async def update_requirement(
    req_id: uuid.UUID, data: RequirementUpdate, admin: AdminUser, db: DbSession
):
    req = await admin_service.get_requirement(db, req_id)
    return await admin_service.update_requirement(
        db, admin, req, data.model_dump(exclude_unset=True)
    )


@router.delete("/requirements/{req_id}", response_model=VisaTypeAdminOut)
async def delete_requirement(req_id: uuid.UUID, admin: AdminUser, db: DbSession):
    """Already-uploaded documents are kept (they move to "other documents")."""
    req = await admin_service.get_requirement(db, req_id)
    return await admin_service.delete_requirement(db, admin, req)
