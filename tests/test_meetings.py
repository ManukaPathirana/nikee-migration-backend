from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserRole
from app.services import email_service
from app.services.meeting_service import send_due_reminders
from tests.conftest import auth_header, create_user, login

API = "/api/v1/meetings"


def _slot(hours_from_now: float, minutes: int = 60) -> dict:
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours_from_now)
    return {
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(minutes=minutes)).isoformat(),
    }


def _emails_to(address: str) -> list[dict]:
    return [m for m in email_service.outbox if address in m["to"]]


@pytest.fixture
async def ctx(client: AsyncClient, db: AsyncSession) -> dict:
    await create_user(db, email="cust@example.com", full_name="Cust Omer")
    await create_user(db, email="s1@example.com", role=UserRole.STAFF, full_name="Staff One")
    await create_user(db, email="s2@example.com", role=UserRole.STAFF, full_name="Staff Two")
    await create_user(db, email="admin@example.com", role=UserRole.ADMIN)
    h = {
        k: auth_header(await login(client, f"{k}@example.com"))
        for k in ("cust", "s1", "s2", "admin")
    }
    resp = await client.post(
        "/api/v1/inquiries",
        json={"subject": "Visa consultation", "description": "I want to discuss my options."},
        headers=h["cust"],
    )
    h["inquiry_id"] = resp.json()["id"]
    await client.post(f"/api/v1/inquiries/{h['inquiry_id']}/claim", headers=h["s1"])
    email_service.outbox.clear()
    return h


async def _propose(client, ctx, slots=None, **extra) -> dict:
    body = {
        "inquiry_id": ctx["inquiry_id"],
        "mode": "online",
        "meeting_link": "https://meet.example.com/abc",
        "slots": slots or [_slot(48), _slot(72)],
        "staff_note": "Bring your passport details",
    } | extra
    return await client.post(API, json=body, headers=ctx["s1"])


async def test_full_flow_confirm_with_ics_and_complete(client: AsyncClient, ctx):
    resp = await _propose(client, ctx)
    assert resp.status_code == 201, resp.text
    meeting = resp.json()
    assert meeting["status"] == "proposed"
    assert len(meeting["slots"]) == 2
    proposal = _emails_to("cust@example.com")[-1]
    assert len(proposal["context"]["slots"]) == 2

    # Customer picks the second slot.
    chosen = meeting["slots"][1]["id"]
    resp = await client.post(
        f"{API}/{meeting['id']}/select-slot", json={"slot_id": chosen}, headers=ctx["cust"]
    )
    assert resp.status_code == 200, resp.text
    confirmed = resp.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_slot"]["id"] == chosen
    assert [s["is_selected"] for s in confirmed["slots"]] == [False, True]

    # Both customer and staff receive the invite with an .ics attachment.
    for address in ("cust@example.com", "s1@example.com"):
        mail = _emails_to(address)[-1]
        assert "confirmed" in mail["subject"].lower()
        ics = mail["attachments"][0]
        assert ics["mime_type"] == "text/calendar"
        body = ics["content"].decode()
        assert "METHOD:REQUEST" in body and "BEGIN:VEVENT" in body
        assert f"UID:meeting-{meeting['id']}@nikeemigration.com" in body
        assert "https://meet.example.com/abc" in body

    # Can't pick again; staff completes it.
    resp = await client.post(
        f"{API}/{meeting['id']}/select-slot", json={"slot_id": chosen}, headers=ctx["cust"]
    )
    assert resp.json()["error"]["code"] == "invalid_status"
    resp = await client.post(f"{API}/{meeting['id']}/complete", headers=ctx["s1"])
    assert resp.json()["status"] == "completed"


async def test_reschedule_then_new_proposal(client: AsyncClient, ctx):
    meeting = (await _propose(client, ctx)).json()
    resp = await client.post(
        f"{API}/{meeting['id']}/request-reschedule",
        json={"note": "Evenings after 6pm please"},
        headers=ctx["cust"],
    )
    assert resp.json()["status"] == "reschedule_requested"
    assert resp.json()["customer_note"] == "Evenings after 6pm please"
    assert "another time" in _emails_to("s1@example.com")[-1]["subject"]

    resp = await client.post(
        f"{API}/{meeting['id']}/propose",
        json={"slots": [_slot(50), _slot(51, 30), _slot(52)]},
        headers=ctx["s1"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "proposed"
    assert len(resp.json()["slots"]) == 3
    old_ids = {s["id"] for s in meeting["slots"]}
    assert old_ids.isdisjoint({s["id"] for s in resp.json()["slots"]})


async def test_cancel_confirmed_sends_ics_cancel(client: AsyncClient, ctx):
    meeting = (await _propose(client, ctx)).json()
    await client.post(
        f"{API}/{meeting['id']}/select-slot",
        json={"slot_id": meeting["slots"][0]["id"]},
        headers=ctx["cust"],
    )
    resp = await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "Consultant unwell"}, headers=ctx["s1"]
    )
    assert resp.json()["status"] == "cancelled"
    mail = _emails_to("cust@example.com")[-1]
    body = mail["attachments"][0]["content"].decode()
    assert "METHOD:CANCEL" in body and "SEQUENCE:1" in body
    # Cancel requires a reason; closed meetings can't be cancelled again.
    resp = await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "again"}, headers=ctx["s1"]
    )
    assert resp.status_code == 400


async def test_validation_rules(client: AsyncClient, ctx):
    resp = await _propose(client, ctx, slots=[_slot(-1)])
    assert resp.json()["error"]["code"] == "slot_in_past"
    resp = await _propose(client, ctx, slots=[_slot(24), _slot(24.5)])
    assert resp.json()["error"]["code"] == "slots_overlap"
    resp = await _propose(client, ctx, slots=[_slot(24), _slot(26), _slot(28), _slot(30)])
    assert resp.status_code == 422  # max 3 slots
    resp = await _propose(client, ctx, mode="in_person", meeting_link=None)
    assert resp.status_code == 422  # in-person needs an address
    resp = await _propose(client, ctx, mode="phone", meeting_link=None)
    assert resp.status_code == 201


async def test_access_control(client: AsyncClient, db, ctx):
    # Only the assigned consultant (or admin) can schedule.
    resp = await client.post(
        API,
        json={"inquiry_id": ctx["inquiry_id"], "mode": "phone", "slots": [_slot(30)]},
        headers=ctx["s2"],
    )
    assert resp.json()["error"]["code"] == "not_assigned"

    meeting = (await _propose(client, ctx)).json()
    await create_user(db, email="other@example.com")
    other = auth_header(await login(client, "other@example.com"))
    assert (await client.get(f"{API}/{meeting['id']}", headers=other)).status_code == 404
    assert (await client.get(f"{API}/{meeting['id']}", headers=ctx["s2"])).status_code == 404
    assert (await client.get(f"{API}/{meeting['id']}", headers=ctx["admin"])).status_code == 200
    # Staff can't pick slots on the customer's behalf; customers can't cancel.
    resp = await client.post(
        f"{API}/{meeting['id']}/select-slot",
        json={"slot_id": meeting["slots"][0]["id"]},
        headers=ctx["s1"],
    )
    assert resp.status_code == 403
    resp = await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "nope"}, headers=ctx["cust"]
    )
    assert resp.status_code == 403

    listing = (await client.get(API, headers=ctx["cust"])).json()
    assert listing["total"] == 1
    assert (await client.get(API, headers=other)).json()["total"] == 0


async def test_admin_flow_assign_then_schedule_single_meeting(client: AsyncClient, db, ctx):
    """Regression: customer inquiry -> admin -> assign -> staff schedules.
    Admin scheduling on an unassigned inquiry used to make the admin the host, and a second
    meeting could be created for the same inquiry."""
    resp = await client.post(
        "/api/v1/inquiries",
        json={"subject": "Second question", "description": "Another question for you."},
        headers=ctx["cust"],
    )
    inquiry_id = resp.json()["id"]
    body = {"inquiry_id": inquiry_id, "mode": "phone", "slots": [_slot(30)]}

    # Admin can't schedule before assigning a consultant.
    resp = await client.post(API, json=body, headers=ctx["admin"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "needs_assignment"

    staff = (await client.get("/api/v1/staff", headers=ctx["admin"])).json()
    s1 = next(s for s in staff if s["email"] == "s1@example.com")
    assert s1["invite_pending"] is False
    await client.post(
        f"/api/v1/inquiries/{inquiry_id}/assign", json={"staff_id": s1["id"]}, headers=ctx["admin"]
    )

    # Admin schedules on behalf of the consultant: the consultant is the host.
    resp = await client.post(API, json=body, headers=ctx["admin"])
    assert resp.status_code == 201, resp.text
    meeting = resp.json()
    assert meeting["staff"]["email"] == "s1@example.com"

    # Only one open meeting per inquiry — new times go on the existing meeting.
    resp = await client.post(API, json=body, headers=ctx["s1"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "meeting_exists"
    resp = await client.post(
        f"{API}/{meeting['id']}/propose", json={"slots": [_slot(40)]}, headers=ctx["s1"]
    )
    assert resp.status_code == 200

    # Reassigning moves the open meeting to the new consultant.
    s2 = next(s for s in staff if s["email"] == "s2@example.com")
    await client.post(
        f"/api/v1/inquiries/{inquiry_id}/assign", json={"staff_id": s2["id"]}, headers=ctx["admin"]
    )
    resp = await client.get(f"{API}/{meeting['id']}", headers=ctx["s2"])
    assert resp.status_code == 200
    assert resp.json()["staff"]["email"] == "s2@example.com"

    # Once the meeting is closed, a new one can be scheduled.
    await client.post(
        f"{API}/{meeting['id']}/cancel", json={"reason": "Customer travelling"}, headers=ctx["s2"]
    )
    resp = await client.post(API, json=body, headers=ctx["s2"])
    assert resp.status_code == 201


async def test_reminders_sent_once_at_24h_and_1h(client: AsyncClient, db: AsyncSession, ctx):
    meeting = (await _propose(client, ctx, slots=[_slot(30)])).json()
    await client.post(
        f"{API}/{meeting['id']}/select-slot",
        json={"slot_id": meeting["slots"][0]["id"]},
        headers=ctx["cust"],
    )
    email_service.outbox.clear()
    start = datetime.fromisoformat(meeting["slots"][0]["start_at"])

    assert await send_due_reminders(db, now=start - timedelta(hours=26)) == 0
    assert await send_due_reminders(db, now=start - timedelta(hours=23)) == 1
    assert await send_due_reminders(db, now=start - timedelta(hours=22)) == 0  # not twice
    assert "24 hours" in _emails_to("cust@example.com")[-1]["subject"]
    assert len(_emails_to("s1@example.com")) == 1

    assert await send_due_reminders(db, now=start - timedelta(minutes=50)) == 1
    assert "an hour" in _emails_to("cust@example.com")[-1]["subject"]
    assert await send_due_reminders(db, now=start - timedelta(minutes=10)) == 0
    assert await send_due_reminders(db, now=start + timedelta(minutes=10)) == 0


async def test_confirming_late_skips_past_reminders(client: AsyncClient, db: AsyncSession, ctx):
    meeting = (await _propose(client, ctx, slots=[_slot(3)])).json()
    await client.post(
        f"{API}/{meeting['id']}/select-slot",
        json={"slot_id": meeting["slots"][0]["id"]},
        headers=ctx["cust"],
    )
    email_service.outbox.clear()
    start = datetime.fromisoformat(meeting["slots"][0]["start_at"])
    # The 24h reminder is skipped (confirmed only 3h before); the 1h one still goes out.
    assert await send_due_reminders(db, now=start - timedelta(hours=2)) == 0
    assert await send_due_reminders(db, now=start - timedelta(minutes=30)) == 1
