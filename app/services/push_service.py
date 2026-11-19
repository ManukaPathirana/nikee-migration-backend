"""Push notifications via Firebase Cloud Messaging (HTTP v1).

Enabled when a Firebase service-account key is configured: FCM_CREDENTIALS_JSON (the JSON
itself, used on Vercel) or FCM_CREDENTIALS_FILE (a path). The project id comes from
FCM_PROJECT_ID or the key. While disabled, in-app notifications still work; nothing is sent.
Android shows them in the notification bar on the CHANNEL_ID channel (created by the app).
"""

import asyncio
import json
import logging
import uuid
from functools import lru_cache
from typing import Any

import httpx
from sqlalchemy import delete, select

from app.core.config import settings

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
CHANNEL_ID = "nikee_updates"  # keep in step with the app (core/push)


@lru_cache
def _key_info() -> dict[str, Any] | None:
    raw = settings.fcm_credentials_json.strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        logger.error("FCM_CREDENTIALS_JSON is not valid JSON; push is off")
        return None
    return info if isinstance(info, dict) and info.get("private_key") else None


def project_id() -> str:
    info = _key_info()
    return settings.fcm_project_id or (info or {}).get("project_id", "")


def is_enabled() -> bool:
    return bool(project_id() and (_key_info() or settings.fcm_credentials_file))


@lru_cache
def _credentials() -> Any:
    from google.oauth2 import service_account

    info = _key_info()
    if info is not None:
        return service_account.Credentials.from_service_account_info(info, scopes=[FCM_SCOPE])
    return service_account.Credentials.from_service_account_file(
        str(settings.resolve_path(settings.fcm_credentials_file)), scopes=[FCM_SCOPE]
    )


def status() -> dict[str, Any]:
    """For the super admin check: configured, and whether Google accepts the key. No secrets."""
    out: dict[str, Any] = {"enabled": is_enabled(), "project_id": project_id(), "key_ok": False}
    if not out["enabled"]:
        out["problem"] = "No Firebase key: set FCM_CREDENTIALS_JSON"
        return out
    try:
        _access_token()
        out["key_ok"] = True
        out["problem"] = None
    except Exception as e:
        out["problem"] = f"{type(e).__name__}: {str(e)[:150]}"
    return out


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
        url = f"https://fcm.googleapis.com/v1/projects/{project_id()}/messages:send"
        sent, stale = 0, []
        async with httpx.AsyncClient(timeout=15) as client:
            for token in tokens:
                message = {
                    "message": {
                        "token": token,
                        "notification": {"title": title, "body": body or ""},
                        "data": data,
                        "android": {
                            "priority": "high",
                            "notification": {
                                "channel_id": CHANNEL_ID,
                                "click_action": "FLUTTER_NOTIFICATION_CLICK",
                            },
                        },
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
