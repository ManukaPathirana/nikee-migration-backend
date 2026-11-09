import uuid
from typing import Any

from app.services import push_service


async def send_push_job(
    ctx: dict[str, Any],
    *,
    user_id: str,
    title: str,
    body: str | None,
    data: dict[str, str],
) -> int:
    return await push_service.send_to_user(uuid.UUID(user_id), title, body, data)
