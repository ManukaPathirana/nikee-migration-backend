from fastapi import APIRouter

from app.core.deps import CurrentUser, DbSession, InternalUser
from app.models import CustomerProfile, UserRole
from app.schemas.attendance import WorkStatusOut
from app.schemas.user import MeOut, MeUpdate
from app.services import attendance_service

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=MeOut)
async def get_me(user: CurrentUser) -> MeOut:
    return MeOut.model_validate(user)


@router.patch("", response_model=MeOut)
async def update_me(data: MeUpdate, user: CurrentUser, db: DbSession) -> MeOut:
    if data.full_name is not None:
        user.full_name = data.full_name.strip()
    if "phone" in data.model_fields_set:
        user.phone = data.phone
    if data.customer_profile is not None and user.role == UserRole.CUSTOMER:
        profile = user.customer_profile or CustomerProfile(user_id=user.id)
        for field in data.customer_profile.model_fields_set:
            setattr(profile, field, getattr(data.customer_profile, field))
        user.customer_profile = profile
    await db.commit()
    await db.refresh(user)
    return MeOut.model_validate(user)


# ---------------------------------------------------------------- work clock (staff + admins)


@router.get("/work", response_model=WorkStatusOut)
async def work_status(user: InternalUser, db: DbSession) -> WorkStatusOut:
    """Am I on the clock, and how long have I worked today / this week."""
    return await attendance_service.status(db, user)


@router.post("/work/start", response_model=WorkStatusOut)
async def start_work(user: InternalUser, db: DbSession) -> WorkStatusOut:
    """Start work. Pressing it again keeps the shift that's already running."""
    return await attendance_service.start_work(db, user)


@router.post("/work/end", response_model=WorkStatusOut)
async def end_work(user: InternalUser, db: DbSession) -> WorkStatusOut:
    """End work. Signing out also ends a running shift."""
    return await attendance_service.end_work(db, user)
