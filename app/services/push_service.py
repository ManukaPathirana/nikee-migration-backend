"""Push notifications via Firebase Cloud Messaging (HTTP v1).

Disabled until both FCM_PROJECT_ID and FCM_CREDENTIALS_FILE (a Firebase service-account JSON)
are set. Enabling also needs `pip install google-auth`. While disabled, in-app notifications
still work; nothing is sent to devices.
"""

import asyncio
import logging
import uuid
from functools import lru_cache
from typing import Any

import httpx
from sqlalchemy import delete, select

from app.core.config import settings

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def is_enabled() -> bool:
    return bool(settings.fcm_project_id and settings.fcm_credentials_file)


@lru_cache
def _credentials() -> Any:
    from google.oauth2 import service_account  # optional dependency

    return service_account.Credentials.from_service_account_file(
        str(settings.resolve_path(settings.fcm_credentials_file)), scopes=[FCM_SCOPE]
    )


def _access_token() -> str:
    from google.auth.transport.requests import Request

    creds = _credentials()
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


async def send_to_user(
    user_id: uuid.UUID, title: str, body: str | None, data: dict[str, str]
) -> int:
    """Send to every registered device of the user. Returns how many were delivered."""
    if not is_enabled():
        return 0
    from app.db.session import SessionLocal
    from app.models import DeviceToken

    async with SessionLocal() as db:
        tokens = list(
            await db.scalars(select(DeviceToken.fcm_token).where(DeviceToken.user_id == user_id))
        )
        if not tokens:
            return 0
        access = await asyncio.to_thread(_access_token)
        url = f"https://fcm.googleapis.com/v1/projects/{settings.fcm_project_id}/messages:send"
        sent, stale = 0, []
        async with httpx.AsyncClient(timeout=15) as client:
            for token in tokens:
                message = {
                    "message": {
                        "token": token,
                        "notification": {"title": title, "body": body or ""},
                        "data": data,
                    }
                }
                resp = await client.post(
                    url, json=message, headers={"Authorization": f"Bearer {access}"}
                )
                if resp.status_code == 200:
                    sent += 1
                elif resp.status_code in (400, 404) and "UNREGISTERED" in resp.text:
                    stale.append(token)
                else:
                    logger.warning("FCM send failed (%s): %s", resp.status_code, resp.text[:200])
        if stale:
            await db.execute(delete(DeviceToken).where(DeviceToken.fcm_token.in_(stale)))
            await db.commit()
        return sent
