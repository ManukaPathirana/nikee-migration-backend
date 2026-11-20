import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeviceToken, UserRole
from app.services import notification_service, push_service
from tests.conftest import TEAM, auth_header, create_user, login


@pytest.fixture
async def h(client: AsyncClient, db: AsyncSession) -> dict:
    await create_user(db, email="cust@example.com")
    await create_user(db, email="other@example.com")
    await create_user(db, email="s1@example.com", role=UserRole.STAFF)
    await create_user(db, email="lead@example.com", role=UserRole.ADMIN)
    return {
        k: auth_header(await login(client, f"{k}@example.com"))
        for k in ("cust", "other", "s1", "lead")
    }


async def _inquiry_with_status_change(client: AsyncClient, h: dict) -> str:
    inquiry = (
        await client.post(
            "/api/v1/inquiries",
            json={
                "visa_type_id": TEAM,
                "subject": "Question",
                "description": "A question about visas.",
            },
            headers=h["cust"],
        )
    ).json()
    await client.post(
        f"/api/v1/inquiries/{inquiry['id']}/status",
        json={"status": "processing"},
        headers=h["lead"],
    )
    return inquiry["id"]


async def test_list_count_and_mark_read(client: AsyncClient, h):
    inquiry_id = await _inquiry_with_status_change(client, h)

    count = (await client.get("/api/v1/notifications/unread-count", headers=h["cust"])).json()
    assert count == {"count": 1}
    listing = (await client.get("/api/v1/notifications", headers=h["cust"])).json()
    note = listing["items"][0]
    assert note["type"] == "inquiry_status"
    assert note["data"]["inquiry_id"] == inquiry_id
    assert note["read_at"] is None

    # Other users can't see or mark it.
    assert (await client.get("/api/v1/notifications", headers=h["other"])).json()["total"] == 0
    resp = await client.post(f"/api/v1/notifications/{note['id']}/read", headers=h["other"])
    assert resp.status_code == 404

    resp = await client.post(f"/api/v1/notifications/{note['id']}/read", headers=h["cust"])
    assert resp.json()["read_at"] is not None
    count = (await client.get("/api/v1/notifications/unread-count", headers=h["cust"])).json()
    assert count == {"count": 0}


async def test_mark_all_read_and_unread_filter(client: AsyncClient, h):
    await _inquiry_with_status_change(client, h)
    await _inquiry_with_status_change(client, h)
    unread = (
        await client.get("/api/v1/notifications", params={"unread": True}, headers=h["cust"])
    ).json()
    assert unread["total"] == 2
    resp = await client.post("/api/v1/notifications/read-all", headers=h["cust"])
    assert "2" in resp.json()["message"]
    unread = (
        await client.get("/api/v1/notifications", params={"unread": True}, headers=h["cust"])
    ).json()
    assert unread["total"] == 0


async def test_device_tokens_register_move_and_remove(client: AsyncClient, db: AsyncSession, h):
    token = "fcm-token-abcdefghijklmnop"
    resp = await client.post(
        "/api/v1/me/device-tokens",
        json={"fcm_token": token, "platform": "android"},
        headers=h["cust"],
    )
    assert resp.status_code == 204
    # Same device, different user signs in: the token moves.
    await client.post(
        "/api/v1/me/device-tokens",
        json={"fcm_token": token, "platform": "android"},
        headers=h["other"],
    )
    rows = (await db.scalars(select(DeviceToken).execution_options(populate_existing=True))).all()
    assert len(rows) == 1
    resp = await client.delete(f"/api/v1/me/device-tokens/{token}", headers=h["other"])
    assert resp.status_code == 204
    assert (await db.scalars(select(DeviceToken))).all() == []


async def test_push_is_queued_after_commit_when_enabled(client: AsyncClient, h, monkeypatch):
    sent: list[dict] = []

    async def fake_send(user_id, title, body, data):
        sent.append({"user_id": str(user_id), "title": title, "data": data})
        return 1

    monkeypatch.setattr(push_service, "is_enabled", lambda: True)
    monkeypatch.setattr(push_service, "send_to_user", fake_send)

    inquiry_id = await _inquiry_with_status_change(client, h)
    if notification_service._tasks:
        await asyncio.gather(*list(notification_service._tasks))
    # The team admin is told about the new inquiry; the customer about the status change.
    assert [p["data"]["type"] for p in sent] == ["inquiry_new", "inquiry_status"]
    assert sent[1]["data"]["inquiry_id"] == inquiry_id


async def test_push_disabled_by_default(client: AsyncClient, h, monkeypatch):
    calls = []
    monkeypatch.setattr(push_service, "send_to_user", lambda *a: calls.append(a))
    await _inquiry_with_status_change(client, h)
    await asyncio.sleep(0)
    assert push_service.is_enabled() is False
    assert calls == []


def test_firebase_key_from_env_json(monkeypatch):
    """On Vercel the key JSON comes from FCM_CREDENTIALS_JSON; its project_id is used."""
    import json

    from app.core.config import settings

    monkeypatch.setattr(settings, "fcm_project_id", "")
    monkeypatch.setattr(settings, "fcm_credentials_file", "")
    push_service._key_info.cache_clear()
    try:
        assert not push_service.is_enabled()
        monkeypatch.setattr(settings, "fcm_credentials_json", "not json")
        push_service._key_info.cache_clear()
        assert not push_service.is_enabled()
        key = {"project_id": "nikee-x", "private_key": "k", "client_email": "push@x.iam"}
        monkeypatch.setattr(settings, "fcm_credentials_json", json.dumps(key))
        push_service._key_info.cache_clear()
        assert push_service.is_enabled() and push_service.project_id() == "nikee-x"
    finally:
        push_service._key_info.cache_clear()


async def test_super_admin_push_test(client: AsyncClient, h, db: AsyncSession, monkeypatch):
    sent: list[tuple] = []

    async def fake_send(user_id, title, body, data):
        sent.append((user_id, title, data["type"]))
        return 1

    monkeypatch.setattr(push_service, "is_enabled", lambda: True)
    monkeypatch.setattr(push_service, "send_to_user", fake_send)
    await create_user(db, email="boss@example.com", role=UserRole.SUPER_ADMIN)
    boss = auth_header(await login(client, "boss@example.com"))
    resp = await client.post(
        "/api/v1/admin/push-test", json={"email": "cust@example.com"}, headers=h["s1"]
    )
    assert resp.status_code == 403
    resp = await client.post(
        "/api/v1/admin/push-test", json={"email": "CUST@example.com"}, headers=boss
    )
    assert resp.status_code == 200, resp.text
    await notification_service.drain_pushes()
    assert [s[1:] for s in sent] == [("Test notification", "test")]
    inbox = (await client.get("/api/v1/notifications", headers=h["cust"])).json()["items"]
    assert inbox[0]["title"] == "Test notification"
