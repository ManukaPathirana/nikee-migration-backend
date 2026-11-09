from fastapi import APIRouter

from app.core.deps import CurrentUser, DbSession
from app.models import CustomerProfile, UserRole
from app.schemas.user import MeOut, MeUpdate

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
