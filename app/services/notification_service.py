"""In-app notifications (+ push once FCM is configured).

`notify()` adds a row to the current transaction. After the transaction commits, a push
job is queued for each notification — but only when push is enabled (see push_service).
"""

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.security import utcnow
from app.models import Notification
from app.schemas.common import PageParams
from app.services import push_service

logger = logging.getLogger(__name__)

_PENDING_KEY = "pending_pushes"
_tasks: set[asyncio.Task[None]] = set()


def notify(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    type: str,
    title: str,
    body: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Add an in-app notification to the current transaction (caller commits)."""
    payload = {k: str(v) for k, v in (data or {}).items()}
    db.add(Notification(user_id=user_id, type=type, title=title, body=body, data=data or {}))
    if push_service.is_enabled():
        db.sync_session.info.setdefault(_PENDING_KEY, []).append(
            {
                "user_id": str(user_id),
                "title": title,
                "body": body,
                "data": payload | {"type": type},
            }
        )


@event.listens_for(Session, "after_commit")
def _queue_pushes(session: Session) -> None:
    pending = session.info.pop(_PENDING_KEY, None)
    if not pending:
        return
    from app.services import job_queue  # avoid an import cycle

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for push in pending:
        task = loop.create_task(job_queue.enqueue("send_push", **push))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


@event.listens_for(Session, "after_rollback")
def _drop_pushes(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


# ---------------------------------------------------------------- reads / updates


async def list_for_user(
    db: AsyncSession, user_id: uuid.UUID, params: PageParams, *, unread_only: bool = False
) -> tuple[list[Notification], int]:
    query = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.scalars(
        query.order_by(Notification.created_at.desc()).offset(params.offset).limit(params.page_size)
    )
    return list(rows), total or 0


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
    ) or 0


async def mark_read(
    db: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification:
    notification = await db.get(Notification, notification_id)
    if notification is None or notification.user_id != user_id:
        raise NotFound("Notification not found")
    if notification.read_at is None:
        notification.read_at = utcnow()
        await db.commit()
    return notification


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=utcnow())
    )
    await db.commit()
    return result.rowcount or 0
