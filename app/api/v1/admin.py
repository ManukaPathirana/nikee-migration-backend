from fastapi import APIRouter

from app.api.v1.company import get_company_row
from app.core.deps import AdminUser, DbSession
from app.models import CompanyInfo
from app.schemas.company import CompanyOut, CompanyUpdate
from app.schemas.inquiry import BoardOut, InquiryOut
from app.services import inquiry_service
from app.services.audit_service import audit

router = APIRouter(prefix="/admin", tags=["admin"])


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


@router.get("/inquiries/board", response_model=BoardOut)
async def inquiry_board(_: AdminUser, db: DbSession) -> BoardOut:
    """Kanban: latest 50 inquiries per status column, plus total counts."""
    columns = await inquiry_service.board(db)
    return BoardOut(
        columns={s: [InquiryOut.model_validate(i) for i in rows] for s, rows in columns.items()},
        counts=await inquiry_service.board_counts(db),
    )
