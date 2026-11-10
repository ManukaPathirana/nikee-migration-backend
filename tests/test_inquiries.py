import re

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, CompanyInfo, Notification, UserRole
from tests.conftest import TEAM, auth_header, create_user, last_email, login

API = "/api/v1/inquiries"


@pytest.fixture
async def people(client: AsyncClient, db: AsyncSession) -> dict:
    await create_user(db, email="cust@example.com", full_name="Cust Omer")
    await create_user(db, email="cust2@example.com", full_name="Other Customer")
    staff1 = await create_user(
        db, email="s1@example.com", role=UserRole.STAFF, full_name="Staff One"
    )
    await create_user(db, email="s2@example.com", role=UserRole.STAFF, full_name="Staff Two")
    await create_user(db, email="admin@example.com", role=UserRole.SUPER_ADMIN, full_name="Ad Min")
    h = {}
    for key, email in [
        ("cust", "cust@example.com"),
        ("cust2", "cust2@example.com"),
        ("s1", "s1@example.com"),
        ("s2", "s2@example.com"),
        ("admin", "admin@example.com"),
    ]:
        h[key] = auth_header(await login(client, email))
    h["s1_id"] = str(staff1.id)
    return h


async def _create(
    client: AsyncClient, headers: dict, subject: str = "Need help with my visa"
) -> dict:
    body = {
        "visa_type_id": TEAM,
        "subject": subject,
        "category": "visa_application",
        "description": "I would like advice on my student visa options.",
        "preferred_contact_time": "Weekdays after 5pm",
    }
    resp = await client.post(API, json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _status(client, headers, inquiry_id, status, reason=None):
    body = {"status": status} | ({"reason": reason} if reason else {})
    return await client.post(f"{API}/{inquiry_id}/status", json=body, headers=headers)


async def test_create_assigns_sequential_ref_numbers(client: AsyncClient, people):
    first = await _create(client, people["cust"])
    second = await _create(client, people["cust2"])
    assert re.fullmatch(r"NM-\d{4}-0001", first["ref_no"])
    assert second["ref_no"].endswith("-0002")
    assert first["status"] == "scheduled"
    assert first["history"][0]["to_status"] == "scheduled"
    assert first["history"][0]["actor"]["full_name"] == "Cust Omer"
    assert last_email("cust@example.com")["context"]["ref_no"] == first["ref_no"]


async def test_customers_only_see_their_own(client: AsyncClient, people):
    mine = await _create(client, people["cust"])
    await _create(client, people["cust2"])
    listing = (await client.get(API, headers=people["cust"])).json()
    assert listing["total"] == 1
    assert (await client.get(f"{API}/{mine['id']}", headers=people["cust2"])).status_code == 404
    # Customers can't change status.
    assert (await _status(client, people["cust"], mine["id"], "processing")).status_code == 403


async def test_queue_claim_and_conflict(client: AsyncClient, people):
    inquiry = await _create(client, people["cust"])
    queue = (await client.get(API, params={"scope": "queue"}, headers=people["s1"])).json()
    assert [i["id"] for i in queue["items"]] == [inquiry["id"]]

    resp = await client.post(f"{API}/{inquiry['id']}/claim", headers=people["s1"])
    assert resp.status_code == 200
    assert resp.json()["assigned_staff"]["full_name"] == "Staff One"

    resp = await client.post(f"{API}/{inquiry['id']}/claim", headers=people["s2"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "already_claimed"

    queue = (await client.get(API, params={"scope": "queue"}, headers=people["s2"])).json()
    assert queue["total"] == 0
    mine = (await client.get(API, params={"scope": "mine"}, headers=people["s1"])).json()
    assert mine["total"] == 1
    # Admins assign rather than claim.
    assert (
        await client.post(f"{API}/{inquiry['id']}/claim", headers=people["admin"])
    ).status_code == 403


async def test_status_transitions_and_history(client: AsyncClient, db: AsyncSession, people):
    inquiry = await _create(client, people["cust"])
    iid = inquiry["id"]

    # scheduled -> completed is not allowed.
    resp = await _status(client, people["s1"], iid, "completed")
    assert resp.json()["error"]["code"] == "invalid_transition"

    # Moving an unclaimed inquiry to processing claims it.
    resp = await _status(client, people["s1"], iid, "processing")
    assert resp.status_code == 200
    assert resp.json()["assigned_staff"]["email"] == "s1@example.com"

    # Another staff member can't act on it.
    resp = await _status(client, people["s2"], iid, "completed")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "not_assigned"

    resp = await _status(client, people["s1"], iid, "completed")
    body = resp.json()
    assert body["status"] == "completed"
    assert body["closed_at"] is not None
    assert [h["to_status"] for h in body["history"]] == ["scheduled", "processing", "completed"]
    assert body["history"][-1]["actor"]["full_name"] == "Staff One"
    assert last_email("cust@example.com")["context"]["status"] == "completed"

    # Final: staff can't change it, admin can reopen with a reason.
    resp = await _status(client, people["s1"], iid, "processing")
    assert resp.json()["error"]["code"] == "inquiry_closed"
    resp = await _status(client, people["admin"], iid, "processing")
    assert resp.json()["error"]["code"] == "reason_required"
    resp = await _status(client, people["admin"], iid, "processing", "Customer has a follow-up")
    assert resp.status_code == 200
    assert resp.json()["closed_at"] is None

    actions = (await db.scalars(select(AuditLog.action).where(AuditLog.entity == "inquiry"))).all()
    assert actions.count("inquiry.status") == 3
    notes = (await db.scalars(select(Notification.type))).all()
    assert "inquiry_status" in notes


async def test_cancel_requires_reason(client: AsyncClient, people):
    inquiry = await _create(client, people["cust"])
    resp = await _status(client, people["s1"], inquiry["id"], "cancelled")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "reason_required"
    resp = await _status(client, people["s1"], inquiry["id"], "cancelled", "Duplicate inquiry")
    assert resp.status_code == 200
    assert resp.json()["history"][-1]["reason"] == "Duplicate inquiry"


async def test_allowed_statuses(client: AsyncClient, people):
    inquiry = await _create(client, people["cust"])
    url = f"{API}/{inquiry['id']}/allowed-statuses"
    assert (await client.get(url, headers=people["s1"])).json() == ["processing", "cancelled"]
    await _status(client, people["s1"], inquiry["id"], "cancelled", "No longer needed")
    assert (await client.get(url, headers=people["s1"])).json() == []
    assert (await client.get(url, headers=people["admin"])).json() == ["scheduled", "processing"]


async def test_notes_internal_hidden_from_customer(client: AsyncClient, people):
    inquiry = await _create(client, people["cust"])
    iid = inquiry["id"]
    await client.post(f"{API}/{iid}/claim", headers=people["s1"])
    await client.post(
        f"{API}/{iid}/notes", json={"body": "Check passport expiry"}, headers=people["s1"]
    )
    await client.post(
        f"{API}/{iid}/notes",
        json={"body": "Hi! I'll call you tomorrow.", "is_internal": False},
        headers=people["s1"],
    )
    # Customer notes are never internal, even if they ask.
    await client.post(
        f"{API}/{iid}/notes", json={"body": "Thanks!", "is_internal": True}, headers=people["cust"]
    )

    customer_view = (await client.get(f"{API}/{iid}", headers=people["cust"])).json()
    assert [n["body"] for n in customer_view["notes"]] == ["Hi! I'll call you tomorrow.", "Thanks!"]
    staff_view = (await client.get(f"{API}/{iid}", headers=people["s1"])).json()
    assert len(staff_view["notes"]) == 3
    # Unassigned staff can't post on someone else's inquiry.
    resp = await client.post(f"{API}/{iid}/notes", json={"body": "x"}, headers=people["s2"])
    assert resp.status_code == 403


async def test_admin_assign_board_and_search(client: AsyncClient, people):
    inquiry = await _create(client, people["cust"], subject="Partner visa question")
    await _create(client, people["cust2"], subject="Visitor visa")

    staff = (await client.get("/api/v1/staff", headers=people["admin"])).json()
    assert {s["full_name"] for s in staff} == {"Staff One", "Staff Two"}

    resp = await client.post(
        f"{API}/{inquiry['id']}/assign", json={"staff_id": people["s1_id"]}, headers=people["admin"]
    )
    assert resp.status_code == 200
    assert resp.json()["assigned_staff"]["email"] == "s1@example.com"
    assert last_email("s1@example.com")["context"]["ref_no"] == inquiry["ref_no"]
    # Only admins can assign.
    resp = await client.post(
        f"{API}/{inquiry['id']}/assign", json={"staff_id": people["s1_id"]}, headers=people["s1"]
    )
    assert resp.status_code == 403

    board = (await client.get("/api/v1/admin/inquiries/board", headers=people["admin"])).json()
    assert board["counts"] == {"scheduled": 2, "processing": 0, "completed": 0, "cancelled": 0}
    assert len(board["columns"]["scheduled"]) == 2
    assert (
        await client.get("/api/v1/admin/inquiries/board", headers=people["s1"])
    ).status_code == 403

    found = (await client.get(API, params={"q": "partner"}, headers=people["admin"])).json()
    assert [i["ref_no"] for i in found["items"]] == [inquiry["ref_no"]]
    found = (await client.get(API, params={"q": "Other Cust"}, headers=people["admin"])).json()
    assert found["total"] == 1


async def test_company_email_can_be_cleared(client: AsyncClient, db: AsyncSession, people):
    db.add(CompanyInfo(name="Nikee Migrations", email="info@example.com"))
    await db.commit()
    resp = await client.put("/api/v1/admin/company", json={"email": ""}, headers=people["admin"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] is None
