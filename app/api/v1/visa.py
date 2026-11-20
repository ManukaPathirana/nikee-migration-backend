import contextlib
import uuid

from fastapi import APIRouter
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.core.deps import DbSession
from app.core.errors import NotFound
from app.models import VisaType
from app.schemas.visa import VisaTypeOut

router = APIRouter(prefix="/visa-types", tags=["visa types"])


def _public(visa: VisaType) -> VisaTypeOut:
    """Only the documents needed to start; the rest are shown in the case once the signed
    contract and the deposit are approved."""
    out = VisaTypeOut.model_validate(visa)
    out.requirements = [r for r in out.requirements if r.is_intake]
    return out


@router.get("", response_model=list[VisaTypeOut])
async def list_visa_types(db: DbSession) -> list[VisaTypeOut]:
    """Public: active visa types with the documents needed to start a case."""
    rows = await db.scalars(
        select(VisaType)
        .where(VisaType.is_active.is_(True))
        .options(selectinload(VisaType.requirements))
        .order_by(VisaType.sort_order, VisaType.name)
    )
    return [_public(v) for v in rows]


@router.get("/{id_or_slug}", response_model=VisaTypeOut)
async def get_visa_type(id_or_slug: str, db: DbSession) -> VisaTypeOut:
    condition = VisaType.slug == id_or_slug
    with contextlib.suppress(ValueError):
        condition = or_(condition, VisaType.id == uuid.UUID(id_or_slug))
    visa = await db.scalar(
        select(VisaType).where(condition).options(selectinload(VisaType.requirements))
    )
    if visa is None or not visa.is_active:
        raise NotFound("Visa type not found")
    return _public(visa)
