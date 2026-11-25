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


def _parse_key(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """(key, problem). Accepts the file content as pasted, also wrapped in quotes."""
    raw = raw.strip()
    if not raw:
        return None, "FCM_CREDENTIALS_JSON is empty or not set for this deployment"
    if raw[0] == "'" and raw[-1] == "'":
        raw = raw[1:-1].strip()
    try:
        info = json.loads(raw)
    except ValueError as e:
        return None, (
            f"FCM_CREDENTIALS_JSON is set ({len(raw)} characters) but isn't valid JSON "
            f"(line {e.lineno}, column {e.colno}): paste the whole key file, starting with {{"
        )
    if isinstance(info, str):  # pasted as a JSON string of the JSON
        return _parse_key(info)
    if not isinstance(info, dict):
        return None, "FCM_CREDENTIALS_JSON must be the key file's JSON object"
    missing = [k for k in ("project_id", "private_key", "client_email") if not info.get(k)]
    if missing:
        return (
            None,
            f"FCM_CREDENTIALS_JSON is missing {', '.join(missing)}: use the service-account key",
        )
    return info, None


@lru_cache
def _key_info() -> dict[str, Any] | None:
    info, problem = _parse_key(settings.fcm_credentials_json)
    if problem and settings.fcm_credentials_json.strip():
        logger.error("Push is off: %s", problem)
    return info


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
        out["problem"] = _parse_key(settings.fcm_credentials_json)[1] or "No Firebase project id"
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


def web_link(role: str, data: dict[str, str]) -> str:
    """Where tapping a web push opens the web app. Mirrors the app's notificationTargetPath."""
    area = {"customer": "customer", "staff": "staff"}.get(role, "admin")
    path = f"/{area}/notifications"
    if data.get("meeting_id"):
        path = f"/{area}/meetings/{data['meeting_id']}"
    elif data.get("inquiry_id"):
        path = f"/{area}/inquiries/{data['inquiry_id']}"
    elif data.get("application_id"):
        app_id = data["application_id"]
        path = {
            "customer": f"/customer/applications/{app_id}",
            "staff": f"/staff/queue/applications/{app_id}",
        }.get(area, f"/admin/applications/{app_id}")
    return f"{settings.web_app_url.rstrip('/')}/#{path}"


async def send_to_user(
    user_id: uuid.UUID, title: str, body: str | None, data: dict[str, str]
) -> int:
    """Send to every registered device of the user. Returns how many were delivered."""
    if not is_enabled():
        return 0
    from app.db.session import SessionLocal
    from app.models import DeviceToken, User

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(DeviceToken.fcm_token, DeviceToken.platform).where(
                    DeviceToken.user_id == user_id
                )
            )
        ).all()
        if not rows:
            return 0
        role = await db.scalar(select(User.role).where(User.id == user_id))
        link = web_link(str(getattr(role, "value", role) or ""), data)
        access = await asyncio.to_thread(_access_token)
        url = f"https://fcm.googleapis.com/v1/projects/{project_id()}/messages:send"
        sent, stale = 0, []
        async with httpx.AsyncClient(timeout=15) as client:
            for token, platform in rows:
                message: dict = {
                    "token": token,
                    "notification": {"title": title, "body": body or ""},
                    "data": data,
                }
                if platform == "web":
                    # Browsers: shown by the web app's service worker; a tap opens the page.
                    message["webpush"] = {
                        "notification": {
                            "icon": f"{settings.web_app_url.rstrip('/')}/icons/Icon-192.png"
                        },
                        "fcm_options": {"link": link},
                    }
                else:
                    message["android"] = {
                        "priority": "high",
                        "notification": {
                            "channel_id": CHANNEL_ID,
                            "click_action": "FLUTTER_NOTIFICATION_CLICK",
                        },
                    }
                message = {"message": message}
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
