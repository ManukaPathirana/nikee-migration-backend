import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.core.deps import DbSession, StaffOrAdmin
from app.core.permissions import team_scope
from app.models import User, UserRole
from app.schemas.inquiry import StaffBrief, TeamBrief

router = APIRouter(prefix="/staff", tags=["staff"])


@router.get("", response_model=list[StaffBrief])
async def list_active_staff(
    user: StaffOrAdmin, db: DbSession, team_id: uuid.UUID | None = None
) -> list[StaffBrief]:
    """Active staff, e.g. for assigning inquiries. Team members see their own team;
    the super admin sees everyone, or one team with `team_id`."""
    query = select(User).where(User.role == UserRole.STAFF, User.is_active.is_(True))
    scope = team_scope(user) or team_id
    if scope is not None:
        query = query.where(User.team_id == scope)
    rows = await db.scalars(query.order_by(User.full_name))
    return [
        StaffBrief(
            id=u.id,
            full_name=u.full_name,
            email=u.email,
            designation=u.staff_profile.designation if u.staff_profile else None,
            invite_pending=u.password_hash is None,
            team=TeamBrief.model_validate(u.team) if u.team else None,
        )
        for u in rows
    ]
