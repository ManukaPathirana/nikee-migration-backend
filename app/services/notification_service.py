import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification


def notify(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    type: str,
    title: str,
    body: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Add an in-app notification to the current transaction (caller commits).

    Push delivery (FCM) hooks in here later.
    """
    db.add(Notification(user_id=user_id, type=type, title=title, body=body, data=data or {}))
