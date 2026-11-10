import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.v1.company import get_company_row
from app.core.deps import AdminUser, DbSession, SuperAdminUser
from app.models import CompanyInfo, UserRole
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
from app.schemas.attendance import AttendanceDetailOut, StaffReportOut
from app.schemas.common import MessageOut, Page, PageParams, page_params
from app.schemas.company import CompanyOut, CompanyUpdate
from app.schemas.inquiry import BoardOut, InquiryOut
from app.schemas.review import ReviewModerate, ReviewOut
from app.services import admin_service, attendance_service, inquiry_service, review_service
from app.services.audit_service import audit

router = APIRouter(prefix="/admin", tags=["admin"])

Params = Annotated[PageParams, Depends(page_params)]
Search = Annotated[str | None, Query(max_length=100)]
# Super admin only; ignored for team admins (always their own team).
TeamFilter = Annotated[uuid.UUID | None, Query()]


# ---------------------------------------------------------------- company


@router.put("/company", response_model=CompanyOut)
async def update_company(data: CompanyUpdate, user: SuperAdminUser, db: DbSession) -> CompanyInfo:
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
async def dashboard(admin: AdminUser, db: DbSession, team_id: TeamFilter = None) -> DashboardOut:
    """Team admin: their team. Super admin: everything plus a per-team overview."""
    return await admin_service.dashboard(db, admin, team_id)


@router.get("/inquiries/board", response_model=BoardOut)
async def inquiry_board(admin: AdminUser, db: DbSession, team_id: TeamFilter = None) -> BoardOut:
    """Kanban: latest 50 inquiries per status column, plus total counts."""
    scope = admin.team_id if admin.role == UserRole.ADMIN else team_id
    columns = await inquiry_service.board(db, team_id=scope)
    return BoardOut(
        columns={s: [InquiryOut.model_validate(i) for i in rows] for s, rows in columns.items()},
        counts=await inquiry_service.board_counts(db, team_id=scope),
    )


# ---------------------------------------------------------------- staff


@router.get("/staff", response_model=Page[StaffOut])
async def list_staff(
    admin: AdminUser,
    db: DbSession,
    params: Params,
    q: Search = None,
    active: bool | None = None,
    team_id: TeamFilter = None,
    role: UserRole | None = None,
) -> Page[StaffOut]:
    """Team admin: their own staff. Super admin: staff and team admins (filter by team/role)."""
    items, total = await admin_service.list_staff(
        db, admin, params, q=q, active=active, team_id=team_id, role=role
    )
    return Page(items=items, total=total, page=params.page, page_size=params.page_size)


@router.post("/staff", response_model=StaffOut, status_code=status.HTTP_201_CREATED)
async def create_staff(data: StaffCreate, admin: AdminUser, db: DbSession) -> StaffOut:
    """Creates the account and emails an invite link to set a password. Team admins add staff
    to their own team; the super admin picks the team and can create team admins."""
    user = await admin_service.create_staff(db, admin, **data.model_dump())
    return await admin_service.staff_out(db, await admin_service.get_staff(db, admin, user.id))


@router.patch("/staff/{staff_id}", response_model=StaffOut)
async def update_staff(
    staff_id: uuid.UUID, data: StaffUpdate, admin: AdminUser, db: DbSession
) -> StaffOut:
    user = await admin_service.get_staff(db, admin, staff_id)
    await admin_service.update_staff(db, admin, user, data.model_dump(exclude_unset=True))
    return await admin_service.staff_out(db, user)


@router.post("/staff/{staff_id}/active", response_model=StaffOut)
async def set_staff_active(
    staff_id: uuid.UUID, data: ActiveIn, admin: AdminUser, db: DbSession
) -> StaffOut:
    """Deactivating signs the person out everywhere. Their open inquiries stay assigned."""
    user = await admin_service.get_staff(db, admin, staff_id)
    await admin_service.set_active(db, admin, user, data.is_active)
    return await admin_service.staff_out(db, user)


@router.post("/staff/{staff_id}/resend-invite", response_model=MessageOut)
async def resend_invite(staff_id: uuid.UUID, admin: AdminUser, db: DbSession) -> MessageOut:
    user = await admin_service.get_staff(db, admin, staff_id)
    await admin_service.resend_invite(db, admin, user)
    return MessageOut(message=f"Invite sent to {user.email}")


# ---------------------------------------------------------------- customers


@router.get("/customers", response_model=Page[CustomerOut])
async def list_customers(
    _: SuperAdminUser, db: DbSession, params: Params, q: Search = None, active: bool | None = None
) -> Page[CustomerOut]:
    items, total = await admin_service.list_customers(db, params, q=q, active=active)
    return Page(items=items, total=total, page=params.page, page_size=params.page_size)


@router.get("/customers/{customer_id}", response_model=CustomerDetail)
async def get_customer(customer_id: uuid.UUID, _: SuperAdminUser, db: DbSession) -> CustomerDetail:
    user = await admin_service.get_customer(db, customer_id)
    return await admin_service.customer_detail(db, user)


@router.post("/customers/{customer_id}/active", response_model=CustomerDetail)
async def set_customer_active(
    customer_id: uuid.UUID, data: ActiveIn, admin: SuperAdminUser, db: DbSession
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
    team_id: TeamFilter = None,
) -> Page[ReviewOut]:
    rows, total = await review_service.list_reviews(
        db,
        admin,
        params,
        rating=rating,
        visible=visible,
        flagged=flagged,
        staff_id=staff_id,
        team_id=team_id,
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


# ---------------------------------------------------------------- work hours


@router.get("/reports/staff", response_model=StaffReportOut)
async def staff_report(
    admin: AdminUser,
    db: DbSession,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    team_id: TeamFilter = None,
) -> StaffReportOut:
    """Hours worked (Start/End work), sign-ins and output per person. Default: this week."""
    return await attendance_service.staff_report(db, admin, start=start, end=end, team_id=team_id)


@router.get("/attendance/{user_id}", response_model=AttendanceDetailOut)
async def attendance_detail(
    user_id: uuid.UUID,
    admin: AdminUser,
    db: DbSession,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
) -> AttendanceDetailOut:
    """One person's shifts and sign-in/sign-out history."""
    return await attendance_service.attendance_detail(db, admin, user_id, start=start, end=end)


# ---------------------------------------------------------------- visa types


@router.get("/visa-types", response_model=list[VisaTypeAdminOut])
async def list_visa_types(_: SuperAdminUser, db: DbSession) -> list:
    """All visa types, including inactive ones."""
    return await admin_service.list_visa_types(db)


@router.post("/visa-types", response_model=VisaTypeAdminOut, status_code=status.HTTP_201_CREATED)
async def create_visa_type(data: VisaTypeCreate, admin: SuperAdminUser, db: DbSession):
    return await admin_service.create_visa_type(db, admin, data.model_dump())


@router.patch("/visa-types/{visa_id}", response_model=VisaTypeAdminOut)
async def update_visa_type(
    visa_id: uuid.UUID, data: VisaTypeUpdate, admin: SuperAdminUser, db: DbSession
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
    visa_id: uuid.UUID, data: RequirementCreate, admin: SuperAdminUser, db: DbSession
):
    visa = await admin_service.get_visa_type(db, visa_id)
    return await admin_service.add_requirement(db, admin, visa, data.model_dump())


@router.patch("/requirements/{req_id}", response_model=VisaTypeAdminOut)
async def update_requirement(
    req_id: uuid.UUID, data: RequirementUpdate, admin: SuperAdminUser, db: DbSession
):
    req = await admin_service.get_requirement(db, req_id)
    return await admin_service.update_requirement(
        db, admin, req, data.model_dump(exclude_unset=True)
    )


@router.delete("/requirements/{req_id}", response_model=VisaTypeAdminOut)
async def delete_requirement(req_id: uuid.UUID, admin: SuperAdminUser, db: DbSession):
    """Already-uploaded documents are kept (they move to "other documents")."""
    req = await admin_service.get_requirement(db, req_id)
    return await admin_service.delete_requirement(db, admin, req)
