from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import UserRole
from app.services import email_service, meeting_service
from app.services.meeting_service import send_due_reminders
from tests.conftest import TEAM, auth_header, create_user, login

API = "/api/v1/meetings"


def _workday(days_ahead: int) -> date:
    d = datetime.now(ZoneInfo(settings.business_timezone)).date() + timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _slot(days_ahead: int = 2, hour: int = 10, minute: int = 0, minutes: int = 30) -> dict:
    """A slot of the shared calendar: business time zone, weekdays 9-5."""
    tz = ZoneInfo(settings.business_timezone)
    start = datetime.combine(_workday(days_ahead), time(hour, minute), tz)
    return {
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(minutes=minutes)).isoformat(),
    }


def _emails_to(address: str) -> list[dict]:
    return [m for m in email_service.outbox if address in m["to"]]


async def _new_inquiry(client: AsyncClient, cust: dict, subject: str = "Visa consultation") -> str:
    resp = await client.post(
        "/api/v1/inquiries",
        json={"visa_type_id": TEAM, "subject": subject, "description": "Let's talk options."},
        headers=cust,
    )
    return resp.json()["id"]


@pytest.fixture
async def ctx(client: AsyncClient, db: AsyncSession) -> dict:
    await create_user(db, email="cust@example.com", full_name="Cust Omer")
    await create_user(db, email="s1@example.com", role=UserRole.STAFF, full_name="Staff One")
    await create_user(db, email="s2@example.com", role=UserRole.STAFF, full_name="Staff Two")
    await create_user(db, email="admin@example.com", role=UserRole.SUPER_ADMIN, full_name="Boss")
    h = {
        k: auth_header(await login(client, f"{k}@example.com"))
        for k in ("cust", "s1", "s2", "admin")
    }
    h["inquiry_id"] = await _new_inquiry(client, h["cust"])
    await client.post(f"/api/v1/inquiries/{h['inquiry_id']}/claim", headers=h["s1"])
    email_service.outbox.clear()
    return h


async def _book(client, ctx, slot=None, headers=None, **extra):
    body = {
        "inquiry_id": ctx["inquiry_id"],
        "mode": "online",
        "meeting_link": "https://meet.example.com/abc",
        "slots": [slot or _slot()],
        "staff_note": "Bring your passport details",
    } | extra
    return await client.post(API, json=body, headers=headers or ctx["s1"])


async def _inbox(client, headers) -> list[dict]:
    return (await client.get("/api/v1/notifications", headers=headers)).json()["items"]


async def test_booking_confirms_and_tells_customer_and_super_admin(client: AsyncClient, ctx):
    resp = await _book(client, ctx)
    assert resp.status_code == 201, resp.text
    meeting = resp.json()
    assert meeting["status"] == "confirmed"
    assert meeting["confirmed_slot"]["start_at"]

    # Customer and super admin get the date, time and link; everyone gets the .ics invite.
    cust = (await _inbox(client, ctx["cust"]))[0]
    assert cust["title"] == "Your meeting is booked"
    assert "https://meet.example.com/abc" in cust["body"]
    boss = (await _inbox(client, ctx["admin"]))[0]
    assert boss["type"] == "meeting_booked" and "https://meet.example.com/abc" in boss["body"]
    for address in ("cust@example.com", "s1@example.com", "admin@example.com"):
        ics = _emails_to(address)[-1]["attachments"][0]["content"].decode()
        assert "METHOD:REQUEST" in ics and f"UID:meeting-{meeting['id']}@" in ics

    resp = await client.post(f"{API}/{meeting['id']}/complete", headers=ctx["s1"])
    assert resp.json()["status"] == "completed"


async def test_shared_calendar_blocks_double_booking_across_teams(
    client: AsyncClient, db: AsyncSession, ctx
):
    slot = _slot(3, 11, 30)
    assert (await _book(client, ctx, slot)).status_code == 201

    # Another staff member's inquiry can't take the same (or an overlapping) time.
    other = await _new_inquiry(client, ctx["cust"], "Second question")
    await client.post(f"/api/v1/inquiries/{other}/claim", headers=ctx["s2"])
    resp = await _book(client, ctx | {"inquiry_id": other}, slot, headers=ctx["s2"])
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "slot_taken"
    resp = await _book(client, ctx | {"inquiry_id": other}, _slot(3, 12), headers=ctx["s2"])
    assert resp.status_code == 201

    # Everyone (staff of any team, admins) sees the busy slots; customers can't.
    cal = await client.get(
        f"{API}/calendar", params={"start": _workday(3).isoformat(), "days": 1}, headers=ctx["s2"]
    )
    assert cal.status_code == 200, cal.text
    day = cal.json()["days"][0]
    assert len(day["slots"]) == 16  # 9:00-17:00 in 30-minute slots
    busy = [s for s in day["slots"] if s["state"] == "busy"]
    assert len(busy) == 2
    first = next(s for s in busy if s["meeting"]["staff_name"] == "Staff One")
    assert first["meeting"]["is_mine"] is False and first["meeting"]["ref_no"] is None
    mine = next(s for s in busy if s["meeting"]["staff_name"] == "Staff Two")
    assert mine["meeting"]["is_mine"] is True and mine["meeting"]["ref_no"]
    assert (await client.get(f"{API}/calendar", headers=ctx["cust"])).status_code == 403


async def test_super_admin_declines_and_staff_rebook(client: AsyncClient, ctx):
    meeting = (await _book(client, ctx, _slot(4, 9))).json()
    # Only the super admin can decline.
    resp = await client.post(
        f"{API}/{meeting['id']}/decline", json={"reason": "In court"}, headers=ctx["s1"]
    )
    assert resp.status_code == 403
    resp = await client.post(
        f"{API}/{meeting['id']}/decline",
        json={"reason": "I'm in court that morning"},
        headers=ctx["admin"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "reschedule_requested"
    assert resp.json()["admin_note"] == "I'm in court that morning"
    staff_note = (await _inbox(client, ctx["s1"]))[0]
    assert staff_note["type"] == "meeting_declined" and "court" in staff_note["body"]
    assert (await _inbox(client, ctx["cust"]))[0]["title"] == "Your meeting time will change"

    # The freed slot can be booked again; staff book a new time for this meeting.
    email_service.outbox.clear()
    resp = await client.post(
        f"{API}/{meeting['id']}/propose", json={"slots": [_slot(4, 14)]}, headers=ctx["s1"]
    )
    assert resp.status_code == 200, resp.text
    rebooked = resp.json()
    assert rebooked["status"] == "confirmed" and rebooked["admin_note"] is None
    assert rebooked["confirmed_slot"]["start_at"] != meeting["confirmed_slot"]["start_at"]
    assert (await _inbox(client, ctx["cust"]))[0]["title"] == "Your meeting time has changed"
    assert (await _inbox(client, ctx["admin"]))[0]["title"].startswith("New time")
    ics = _emails_to("cust@example.com")[-1]["attachments"][0]["content"].decode()
    assert "SEQUENCE:1" in ics  # replaces the earlier invite


async def test_customer_asks_for_another_time(client: AsyncClient, ctx):
    meeting = (await _book(client, ctx)).json()
    resp = await client.post(
        f"{API}/{meeting['id']}/request-reschedule",
        json={"note": "Afternoons please"},
        headers=ctx["cust"],
    )
    assert resp.json()["status"] == "reschedule_requested"
    assert "another time" in _emails_to("s1@example.com")[-1]["subject"]
    resp = await client.post(
        f"{API}/{meeting['id']}/propose", json={"slots": [_slot(2, 15)]}, headers=ctx["s1"]
    )
    assert resp.json()["status"] == "confirmed"


async def test_cancel_confirmed_sends_ics_cancel(client: AsyncClient, ctx):
    meeting = (await _book(client, ctx)).json()
    resp = await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "Consultant unwell"}, headers=ctx["s1"]
    )
    assert resp.json()["status"] == "cancelled"
    body = _emails_to("cust@example.com")[-1]["attachments"][0]["content"].decode()
    assert "METHOD:CANCEL" in body and "SEQUENCE:1" in body
    resp = await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "again"}, headers=ctx["s1"]
    )
    assert resp.status_code == 400


async def test_validation_rules(client: AsyncClient, ctx):
    resp = await _book(client, ctx, _slot(-3))
    assert resp.json()["error"]["code"] in ("slot_in_past", "outside_hours")
    assert (await _book(client, ctx, _slot(2, 8, 30))).json()["error"]["code"] == "outside_hours"
    assert (await _book(client, ctx, _slot(2, 17))).json()["error"]["code"] == "outside_hours"
    resp = await _book(client, ctx, _slot(2, 10, minutes=60))
    assert resp.json()["error"]["code"] == "invalid_slot_length"
    assert (await _book(client, ctx, _slot(2, 10, 15))).json()["error"]["code"] == "invalid_slot"
    saturday = _workday(1)
    while saturday.weekday() != 5:
        saturday += timedelta(days=1)
    tz = ZoneInfo(settings.business_timezone)
    start = datetime.combine(saturday, time(10), tz)
    weekend = {"start_at": start.isoformat(), "end_at": (start + timedelta(minutes=30)).isoformat()}
    assert (await _book(client, ctx, weekend)).json()["error"]["code"] == "outside_hours"
    body = {"slots": [_slot(2, 10), _slot(2, 11)]}
    resp = await _book(client, ctx, **body)
    assert resp.json()["error"]["code"] == "one_slot"
    resp = await _book(client, ctx, mode="in_person", meeting_link=None)
    assert resp.status_code == 422  # in-person needs an address
    resp = await _book(client, ctx, mode="phone", meeting_link=None)
    assert resp.status_code == 201


async def test_access_control(client: AsyncClient, db, ctx):
    resp = await client.post(
        API,
        json={"inquiry_id": ctx["inquiry_id"], "mode": "phone", "slots": [_slot()]},
        headers=ctx["s2"],
    )
    assert resp.json()["error"]["code"] == "not_assigned"

    meeting = (await _book(client, ctx)).json()
    await create_user(db, email="other@example.com")
    other = auth_header(await login(client, "other@example.com"))
    assert (await client.get(f"{API}/{meeting['id']}", headers=other)).status_code == 404
    assert (await client.get(f"{API}/{meeting['id']}", headers=ctx["s2"])).status_code == 404
    assert (await client.get(f"{API}/{meeting['id']}", headers=ctx["admin"])).status_code == 200
    resp = await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "nope"}, headers=ctx["cust"]
    )
    assert resp.status_code == 403
    assert (await client.get(API, headers=ctx["cust"])).json()["total"] == 1
    assert (await client.get(API, headers=other)).json()["total"] == 0


async def test_admin_assigns_then_books_single_meeting(client: AsyncClient, db, ctx):
    inquiry_id = await _new_inquiry(client, ctx["cust"], "Second question")
    body = {"inquiry_id": inquiry_id, "mode": "phone", "slots": [_slot(5, 10)]}

    resp = await client.post(API, json=body, headers=ctx["admin"])
    assert resp.json()["error"]["code"] == "needs_assignment"

    staff = (await client.get("/api/v1/staff", headers=ctx["admin"])).json()
    s1 = next(s for s in staff if s["email"] == "s1@example.com")
    await client.post(
        f"/api/v1/inquiries/{inquiry_id}/assign", json={"staff_id": s1["id"]}, headers=ctx["admin"]
    )
    resp = await client.post(API, json=body, headers=ctx["admin"])
    assert resp.status_code == 201, resp.text
    meeting = resp.json()
    assert meeting["staff"]["email"] == "s1@example.com"
    # Booked by someone else: the consultant is told.
    assert (await _inbox(client, ctx["s1"]))[0]["type"] == "meeting_booked"

    resp = await client.post(API, json=body | {"slots": [_slot(5, 11)]}, headers=ctx["s1"])
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "meeting_exists"

    s2 = next(s for s in staff if s["email"] == "s2@example.com")
    await client.post(
        f"/api/v1/inquiries/{inquiry_id}/assign", json={"staff_id": s2["id"]}, headers=ctx["admin"]
    )
    resp = await client.get(f"{API}/{meeting['id']}", headers=ctx["s2"])
    assert resp.json()["staff"]["email"] == "s2@example.com"
    await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "Customer travelling"}, headers=ctx["s2"]
    )
    resp = await client.post(API, json=body, headers=ctx["s2"])
    assert resp.status_code == 201  # the cancelled meeting's slot is free again


async def test_reminders_sent_once_at_24h_and_1h(client: AsyncClient, db: AsyncSession, ctx):
    meeting = (await _book(client, ctx, _slot(3))).json()
    email_service.outbox.clear()
    start = datetime.fromisoformat(meeting["confirmed_slot"]["start_at"])

    assert await send_due_reminders(db, now=start - timedelta(hours=26)) == 0
    assert await send_due_reminders(db, now=start - timedelta(hours=23)) == 1
    assert await send_due_reminders(db, now=start - timedelta(hours=22)) == 0  # not twice
    assert "24 hours" in _emails_to("cust@example.com")[-1]["subject"]
    assert len(_emails_to("s1@example.com")) == 1
    reminders = [n for n in await _inbox(client, ctx["admin"]) if n["type"] == "meeting_reminder"]
    assert reminders  # the super admin is reminded too

    assert await send_due_reminders(db, now=start - timedelta(minutes=50)) == 1
    assert "an hour" in _emails_to("cust@example.com")[-1]["subject"]
    assert await send_due_reminders(db, now=start + timedelta(minutes=10)) == 0


async def test_booking_late_skips_past_reminders(
    client: AsyncClient, db: AsyncSession, ctx, monkeypatch
):
    slot = _slot(3, 13)
    start = datetime.fromisoformat(slot["start_at"])
    monkeypatch.setattr(meeting_service, "utcnow", lambda: start - timedelta(hours=3))
    assert (await _book(client, ctx, slot)).status_code == 201
    monkeypatch.undo()
    email_service.outbox.clear()
    # Booked only 3h before: the 24h reminder is skipped; the 1h one still goes out.
    assert await send_due_reminders(db, now=start - timedelta(hours=2)) == 0
    assert await send_due_reminders(db, now=start - timedelta(minutes=30)) == 1
