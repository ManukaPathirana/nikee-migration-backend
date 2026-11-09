import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import delete, select

from app.core.deps import CurrentUser, DbSession
from app.models import DeviceToken
from app.schemas.common import MessageOut, Page, PageParams, page_params
from app.schemas.notification import DeviceTokenIn, NotificationOut, UnreadCountOut
from app.services import notification_service as svc

router = APIRouter(tags=["notifications"])


@router.get("/notifications", response_model=Page[NotificationOut])
async def list_notifications(
    user: CurrentUser,
    db: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    unread: bool = False,
) -> Page[NotificationOut]:
    rows, total = await svc.list_for_user(db, user.id, params, unread_only=unread)
    return Page(
        items=[NotificationOut.model_validate(n) for n in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/notifications/unread-count", response_model=UnreadCountOut)
async def unread_count(user: CurrentUser, db: DbSession) -> UnreadCountOut:
    return UnreadCountOut(count=await svc.unread_count(db, user.id))


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> NotificationOut:
    return NotificationOut.model_validate(await svc.mark_read(db, user.id, notification_id))


@router.post("/notifications/read-all", response_model=MessageOut)
async def mark_all_read(user: CurrentUser, db: DbSession) -> MessageOut:
    count = await svc.mark_all_read(db, user.id)
    return MessageOut(message=f"Marked {count} notification(s) as read")


@router.post("/me/device-tokens", status_code=status.HTTP_204_NO_CONTENT)
async def register_device(data: DeviceTokenIn, user: CurrentUser, db: DbSession) -> Response:
    """Register this device for push. Re-registering moves the token to the current user."""
    existing = await db.scalar(select(DeviceToken).where(DeviceToken.fcm_token == data.fcm_token))
    if existing is None:
        db.add(DeviceToken(user_id=user.id, fcm_token=data.fcm_token, platform=data.platform))
    else:
        existing.user_id = user.id
        existing.platform = data.platform
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/me/device-tokens/{fcm_token}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(fcm_token: str, user: CurrentUser, db: DbSession) -> Response:
    """Call on sign-out so the device stops receiving this user's pushes."""
    await db.execute(
        delete(DeviceToken).where(
            DeviceToken.fcm_token == fcm_token, DeviceToken.user_id == user.id
        )
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
