from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import UserRole, VisaType
from app.services import email_service, meeting_service
from app.services.meeting_service import send_due_reminders
from tests.conftest import TEAM, auth_header, create_user, login

API = "/api/v1/meetings"


def _workday(days_ahead: int) -> date:
    d = datetime.now(ZoneInfo(settings.business_timezone)).date() + timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _slot(days_ahead: int = 2, hour: int = 10, minute: int = 0, minutes: int = 60) -> dict:
    """A meeting time: two 30-minute slots (1 hour), business time zone, weekdays 9-5."""
    tz = ZoneInfo(settings.business_timezone)
    start = datetime.combine(_workday(days_ahead), time(hour, minute), tz)
    return {
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(minutes=minutes)).isoformat(),
    }


def _emails_to(address: str) -> list[dict]:
    return [m for m in email_service.outbox if address in m["to"]]


async def _new_inquiry(
    client: AsyncClient,
    cust: dict,
    subject: str = "Visa consultation",
    *,
    team: str = TEAM,
    kind: str = "consultation",
    paid_by: dict | None = None,
) -> str:
    resp = await client.post(
        "/api/v1/inquiries",
        json={
            "visa_type_id": team,
            "kind": kind,
            "subject": subject,
            "description": "Let's talk options.",
        },
        headers=cust,
    )
    assert resp.status_code == 201, resp.text
    iid = resp.json()["id"]
    if kind == "consultation" and paid_by:
        await pay_consultation_fee(client, iid, cust, paid_by)
    return iid


async def pay_consultation_fee(client: AsyncClient, iid: str, cust: dict, admin: dict) -> None:
    """Fee set by the admin, receipt uploaded by the customer, approved: meeting can be booked."""
    base = f"/api/v1/inquiries/{iid}"
    r = await client.post(f"{base}/fee", json={"amount": "150"}, headers=admin)
    assert r.status_code == 200, r.text
    files = {"file": ("r.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40, "image/png")}
    r = await client.post(f"{base}/fee-receipt", files=files, headers=cust)
    assert r.status_code == 200, r.text
    r = await client.post(f"{base}/fee/review", json={"approve": True}, headers=admin)
    assert r.status_code == 200, r.text


@pytest.fixture
async def ctx(client: AsyncClient, db: AsyncSession) -> dict:
    """Default team with its admin (lead) and a staff member, the Appeal team with its admin,
    the super admin (the migration agent who hosts consultations) and a customer."""
    appeal = VisaType(name="Appeal", slug="appeal", sort_order=1)
    db.add(appeal)
    await db.commit()
    await create_user(db, email="cust@example.com", full_name="Cust Omer")
    await create_user(db, email="s1@example.com", role=UserRole.STAFF, full_name="Staff One")
    await create_user(db, email="lead@example.com", role=UserRole.ADMIN, full_name="Team Lead")
    await create_user(
        db,
        email="lead2@example.com",
        role=UserRole.ADMIN,
        full_name="Appeal Lead",
        team_id=appeal.id,
    )
    await create_user(db, email="admin@example.com", role=UserRole.SUPER_ADMIN, full_name="Boss")
    h: dict = {
        k: auth_header(await login(client, f"{k}@example.com"))
        for k in ("cust", "s1", "lead", "lead2", "admin")
    }
    h["appeal"] = str(appeal.id)
    h["inquiry_id"] = await _new_inquiry(client, h["cust"], paid_by=h["admin"])
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
    return await client.post(API, json=body, headers=headers or ctx["lead"])


async def _inbox(client, headers) -> list[dict]:
    return (await client.get("/api/v1/notifications", headers=headers)).json()["items"]


async def test_team_admin_books_consultation_hosted_by_super_admin(client: AsyncClient, ctx):
    resp = await _book(client, ctx)
    assert resp.status_code == 201, resp.text
    meeting = resp.json()
    assert meeting["status"] == "confirmed"
    # No staff member: the migration agent (super admin) hosts every consultation.
    assert meeting["staff"]["email"] == "admin@example.com"

    cust = (await _inbox(client, ctx["cust"]))[0]
    assert cust["title"] == "Your meeting is booked"
    assert "https://meet.example.com/abc" in cust["body"]
    boss = [n for n in await _inbox(client, ctx["admin"]) if (n["data"] or {}).get("meeting_id")]
    assert len(boss) == 1 and "https://meet.example.com/abc" in boss[0]["body"]
    for address in ("cust@example.com", "admin@example.com"):
        mails = _emails_to(address)
        assert len(mails) == 1  # one invite each, no duplicates
        ics = mails[0]["attachments"][0]["content"].decode()
        assert "METHOD:REQUEST" in ics and f"UID:meeting-{meeting['id']}@" in ics

    # The super admin sees it in "my meetings".
    mine = (await client.get(API, params={"host": "me"}, headers=ctx["admin"])).json()
    assert [m["id"] for m in mine["items"]] == [meeting["id"]]
    assert (await client.get(API, params={"host": "me"}, headers=ctx["lead"])).json()["total"] == 0

    resp = await client.post(f"{API}/{meeting['id']}/complete", headers=ctx["admin"])
    assert resp.json()["status"] == "completed"


async def test_staff_cant_book_and_agent_calendar_is_shared_by_all_teams(client: AsyncClient, ctx):
    resp = await _book(client, ctx, headers=ctx["s1"])
    assert resp.status_code in (403, 404)

    slot = _slot(3, 11, 30)
    assert (await _book(client, ctx, slot)).status_code == 201
    # Another team's consultation can't take the same or an overlapping time.
    other = await _new_inquiry(
        client, ctx["cust"], "Appeal question", team=ctx["appeal"], paid_by=ctx["admin"]
    )
    other_ctx = ctx | {"inquiry_id": other}
    resp = await _book(client, other_ctx, slot, headers=ctx["lead2"])
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "slot_taken"
    resp = await _book(client, other_ctx, _slot(3, 12), headers=ctx["lead2"])
    assert resp.status_code == 409  # 12:00 overlaps the second half of 11:30-12:30
    resp = await _book(client, other_ctx, _slot(3, 12, 30), headers=ctx["lead2"])
    assert resp.status_code == 201

    cal = await client.get(
        f"{API}/calendar",
        params={"start": _workday(3).isoformat(), "days": 1},
        headers=ctx["lead2"],
    )
    day = cal.json()["days"][0]
    assert len(day["slots"]) == 16
    busy = [s for s in day["slots"] if s["state"] == "busy"]
    assert len(busy) == 4  # two 1-hour meetings
    theirs = [s for s in busy if s["meeting"]["team"] != "Appeal"]
    assert theirs and theirs[0]["meeting"]["ref_no"] is None  # other team: no details
    mine = [s for s in busy if s["meeting"]["team"] == "Appeal"]
    assert mine and mine[0]["meeting"]["is_mine"] is True and mine[0]["meeting"]["ref_no"]
    assert (await client.get(f"{API}/calendar", headers=ctx["cust"])).status_code == 403


async def test_super_admin_declines_and_team_admin_rebooks(client: AsyncClient, ctx):
    meeting = (await _book(client, ctx, _slot(4, 9))).json()
    resp = await client.post(
        f"{API}/{meeting['id']}/decline", json={"reason": "In court"}, headers=ctx["lead"]
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
    # The team admin (who books) is asked to pick another time; the customer is told.
    note = (await _inbox(client, ctx["lead"]))[0]
    assert note["type"] == "meeting_declined" and "court" in note["body"]
    assert (await _inbox(client, ctx["cust"]))[0]["title"] == "Your meeting time will change"

    email_service.outbox.clear()
    resp = await client.post(
        f"{API}/{meeting['id']}/propose", json={"slots": [_slot(4, 14)]}, headers=ctx["lead"]
    )
    assert resp.status_code == 200, resp.text
    rebooked = resp.json()
    assert rebooked["status"] == "confirmed" and rebooked["admin_note"] is None
    assert (await _inbox(client, ctx["cust"]))[0]["title"] == "Your meeting time has changed"
    ics = _emails_to("cust@example.com")[-1]["attachments"][0]["content"].decode()
    assert "SEQUENCE:1" in ics


async def test_customer_asks_for_another_time(client: AsyncClient, ctx):
    meeting = (await _book(client, ctx)).json()
    resp = await client.post(
        f"{API}/{meeting['id']}/request-reschedule",
        json={"note": "Afternoons please"},
        headers=ctx["cust"],
    )
    assert resp.json()["status"] == "reschedule_requested"
    lead_inbox = await _inbox(client, ctx["lead"])
    assert any(n["type"] == "meeting_reschedule_requested" for n in lead_inbox)
    resp = await client.post(
        f"{API}/{meeting['id']}/propose", json={"slots": [_slot(2, 15)]}, headers=ctx["lead"]
    )
    assert resp.json()["status"] == "confirmed"


async def test_cancel_confirmed_sends_ics_cancel(client: AsyncClient, ctx):
    meeting = (await _book(client, ctx)).json()
    resp = await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "Customer unwell"}, headers=ctx["lead"]
    )
    assert resp.json()["status"] == "cancelled"
    cancelled = [n for n in await _inbox(client, ctx["admin"]) if n["type"] == "meeting_cancelled"]
    assert cancelled and "free again" in cancelled[0]["body"]
    body = _emails_to("cust@example.com")[-1]["attachments"][0]["content"].decode()
    assert "METHOD:CANCEL" in body and "SEQUENCE:1" in body
    resp = await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "again"}, headers=ctx["lead"]
    )
    assert resp.status_code == 400


async def test_validation_rules(client: AsyncClient, ctx):
    resp = await _book(client, ctx, _slot(-3))
    assert resp.json()["error"]["code"] in ("slot_in_past", "outside_hours")
    assert (await _book(client, ctx, _slot(2, 8, 30))).json()["error"]["code"] == "outside_hours"
    assert (await _book(client, ctx, _slot(2, 16, 30))).json()["error"]["code"] == "outside_hours"
    resp = await _book(client, ctx, _slot(2, 10, minutes=30))
    assert resp.json()["error"]["code"] == "invalid_slot_length"
    assert (await _book(client, ctx, _slot(2, 10, 15))).json()["error"]["code"] == "invalid_slot"
    saturday = _workday(1)
    while saturday.weekday() != 5:
        saturday += timedelta(days=1)
    tz = ZoneInfo(settings.business_timezone)
    start = datetime.combine(saturday, time(10), tz)
    weekend = {"start_at": start.isoformat(), "end_at": (start + timedelta(minutes=60)).isoformat()}
    assert (await _book(client, ctx, weekend)).json()["error"]["code"] == "outside_hours"
    resp = await _book(client, ctx, slots=[_slot(2, 10), _slot(2, 11)])
    assert resp.json()["error"]["code"] == "one_slot"
    resp = await _book(client, ctx, mode="in_person", meeting_link=None)
    assert resp.status_code == 422  # in-person needs an address
    resp = await _book(client, ctx, mode="phone", meeting_link=None)
    assert resp.status_code == 201


async def test_access_control_and_one_open_meeting(client: AsyncClient, db, ctx):
    meeting = (await _book(client, ctx)).json()
    await create_user(db, email="other@example.com")
    other = auth_header(await login(client, "other@example.com"))
    assert (await client.get(f"{API}/{meeting['id']}", headers=other)).status_code == 404
    assert (await client.get(f"{API}/{meeting['id']}", headers=ctx["lead2"])).status_code == 404
    assert (await client.get(f"{API}/{meeting['id']}", headers=ctx["admin"])).status_code == 200
    resp = await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "nope"}, headers=ctx["cust"]
    )
    assert resp.status_code == 403
    assert (await client.get(API, headers=ctx["cust"])).json()["total"] == 1
    # One open meeting per inquiry.
    resp = await _book(client, ctx, _slot(5, 11))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "meeting_exists"


async def test_reminders_sent_once_at_24h_and_1h(client: AsyncClient, db: AsyncSession, ctx):
    meeting = (await _book(client, ctx, _slot(3))).json()
    email_service.outbox.clear()
    start = datetime.fromisoformat(meeting["confirmed_slot"]["start_at"])

    assert await send_due_reminders(db, now=start - timedelta(hours=26)) == 0
    assert await send_due_reminders(db, now=start - timedelta(hours=23)) == 1
    assert await send_due_reminders(db, now=start - timedelta(hours=22)) == 0  # not twice
    assert "24 hours" in _emails_to("cust@example.com")[-1]["subject"]
    assert len(_emails_to("admin@example.com")) == 1
    reminders = [n for n in await _inbox(client, ctx["admin"]) if n["type"] == "meeting_reminder"]
    assert len(reminders) == 1  # host and super admin are the same person: one reminder

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
    assert await send_due_reminders(db, now=start - timedelta(hours=2)) == 0
    assert await send_due_reminders(db, now=start - timedelta(minutes=30)) == 1


async def test_general_inquiry_meets_the_team_admin_in_their_own_calendar(client: AsyncClient, ctx):
    gid = await _new_inquiry(client, ctx["cust"], "Question about my case", kind="general")
    assert (await client.get(f"/api/v1/inquiries/{gid}", headers=ctx["s1"])).status_code == 404
    body = {"inquiry_id": gid, "mode": "phone", "slots": [_slot(3, 11, 30)]}
    assert (await client.post(API, json=body, headers=ctx["admin"])).status_code == 403

    # A consultation at 11:30 in the agent's calendar doesn't block the team admin's calendar.
    assert (await _book(client, ctx, _slot(3, 11, 30))).status_code == 201
    email_service.outbox.clear()
    resp = await client.post(API, json=body, headers=ctx["lead"])
    assert resp.status_code == 201, resp.text
    meeting = resp.json()
    assert meeting["staff"]["email"] == "lead@example.com"  # the team admin hosts it
    assert meeting["inquiry"]["kind"] == "general"
    boss = await _inbox(client, ctx["admin"])
    assert all((n.get("data") or {}).get("meeting_id") != meeting["id"] for n in boss)
    assert not _emails_to("admin@example.com")

    day = _workday(3).isoformat()
    team_cal = await client.get(
        f"{API}/calendar", params={"start": day, "days": 1, "team_id": TEAM}, headers=ctx["lead"]
    )
    agent_cal = await client.get(
        f"{API}/calendar", params={"start": day, "days": 1}, headers=ctx["lead"]
    )
    team_busy = [s for s in team_cal.json()["days"][0]["slots"] if s["state"] == "busy"]
    agent_busy = [s for s in agent_cal.json()["days"][0]["slots"] if s["state"] == "busy"]
    assert [s["meeting"]["id"] for s in team_busy] == [meeting["id"]] * 2
    assert all(s["meeting"]["id"] != meeting["id"] for s in agent_busy) and len(agent_busy) == 2
    # Another team can't open this team's calendar.
    resp = await client.get(f"{API}/calendar", params={"team_id": TEAM}, headers=ctx["lead2"])
    assert resp.status_code == 404

    other = await _new_inquiry(client, ctx["cust"], "Another question", kind="general")
    resp = await client.post(
        API, json=body | {"inquiry_id": other, "slots": [_slot(3, 12)]}, headers=ctx["lead"]
    )
    assert resp.json()["error"]["code"] == "slot_taken"
    resp = await client.post(
        f"{API}/{meeting['id']}/decline", json={"reason": "Busy then"}, headers=ctx["admin"]
    )
    assert resp.json()["error"]["code"] == "not_consultation"
