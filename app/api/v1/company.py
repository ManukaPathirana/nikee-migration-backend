from fastapi import APIRouter
from sqlalchemy import select

from app.core.deps import DbSession
from app.core.errors import NotFound
from app.models import CompanyInfo
from app.schemas.company import CompanyOut

router = APIRouter(prefix="/company", tags=["company"])


async def get_company_row(db: DbSession) -> CompanyInfo:
    company = await db.scalar(select(CompanyInfo).order_by(CompanyInfo.created_at).limit(1))
    if company is None:
        raise NotFound("Company information has not been set up")
    return company


@router.get("", response_model=CompanyOut)
async def get_company(db: DbSession) -> CompanyInfo:
    """Public: company details shown in the app."""
    return await get_company_row(db)
