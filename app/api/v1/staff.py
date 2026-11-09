from fastapi import APIRouter
from sqlalchemy import select

from app.core.deps import DbSession, StaffOrAdmin
from app.models import User, UserRole
from app.schemas.inquiry import StaffBrief

router = APIRouter(prefix="/staff", tags=["staff"])


@router.get("", response_model=list[StaffBrief])
async def list_active_staff(_: StaffOrAdmin, db: DbSession) -> list[StaffBrief]:
    """Active staff members, e.g. for assigning inquiries."""
    rows = await db.scalars(
        select(User)
        .where(User.role == UserRole.STAFF, User.is_active.is_(True))
        .order_by(User.full_name)
    )
    return [
        StaffBrief(
            id=u.id,
            full_name=u.full_name,
            email=u.email,
            designation=u.staff_profile.designation if u.staff_profile else None,
        )
        for u in rows
    ]
