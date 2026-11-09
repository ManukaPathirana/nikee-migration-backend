import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, DocumentRequirement, UserRole, VisaType
from tests.conftest import PASSWORD, auth_header, create_user, last_email, login

ADMIN = "/api/v1/admin"


@pytest.fixture
async def h(client: AsyncClient, db: AsyncSession) -> dict:
    await create_user(db, email="admin@example.com", role=UserRole.ADMIN, full_name="Ad Min")
    await create_user(db, email="cust@example.com", full_name="Jane Perera")
    await create_user(db, email="s1@example.com", role=UserRole.STAFF, full_name="Staff One")
    return {
        k: auth_header(await login(client, f"{k}@example.com")) for k in ("admin", "cust", "s1")
    }


async def _completed_inquiry(client: AsyncClient, h: dict) -> dict:
    inquiry = (
        await client.post(
            "/api/v1/inquiries",
            json={"subject": "Visa advice", "description": "Please help with my visa."},
            headers=h["cust"],
        )
    ).json()
    iid = inquiry["id"]
    await client.post(
        f"/api/v1/inquiries/{iid}/status", json={"status": "processing"}, headers=h["s1"]
    )
    await client.post(
        f"/api/v1/inquiries/{iid}/status", json={"status": "completed"}, headers=h["s1"]
    )
    return inquiry


async def test_staff_lifecycle_invite_edit_deactivate(client: AsyncClient, db: AsyncSession, h):
    body = {"email": "New.Staff@Example.com", "full_name": "New Staff", "designation": "Consultant"}
    resp = await client.post(f"{ADMIN}/staff", json=body, headers=h["admin"])
    assert resp.status_code == 201, resp.text
    staff = resp.json()
    assert staff["email"] == "new.staff@example.com"
    assert staff["invite_pending"] is True
    token = last_email("new.staff@example.com")["context"]["token"]

    # Duplicate email is rejected; only admins can manage staff.
    assert (await client.post(f"{ADMIN}/staff", json=body, headers=h["admin"])).status_code == 409
    assert (await client.post(f"{ADMIN}/staff", json=body, headers=h["s1"])).status_code == 403

    # Resend invite works until the invite is accepted; the old link stops working.
    resp = await client.post(f"{ADMIN}/staff/{staff['id']}/resend-invite", headers=h["admin"])
    assert resp.status_code == 200
    new_token = last_email("new.staff@example.com")["context"]["token"]
    assert new_token != token
    resp = await client.post(
        "/api/v1/auth/accept-invite", json={"token": token, "password": PASSWORD}
    )
    assert resp.status_code == 400
    resp = await client.post(
        "/api/v1/auth/accept-invite", json={"token": new_token, "password": PASSWORD}
    )
    assert resp.status_code == 200
    resp = await client.post(f"{ADMIN}/staff/{staff['id']}/resend-invite", headers=h["admin"])
    assert resp.json()["error"]["code"] == "invite_accepted"

    resp = await client.patch(
        f"{ADMIN}/staff/{staff['id']}", json={"specialization": "Student visas"}, headers=h["admin"]
    )
    assert resp.json()["specialization"] == "Student visas"

    listing = (await client.get(f"{ADMIN}/staff", params={"q": "new"}, headers=h["admin"])).json()
    assert [s["full_name"] for s in listing["items"]] == ["New Staff"]

    # Deactivating signs them out and blocks login.
    session = await login(client, "new.staff@example.com")
    resp = await client.post(
        f"{ADMIN}/staff/{staff['id']}/active", json={"is_active": False}, headers=h["admin"]
    )
    assert resp.json()["is_active"] is False
    assert (await client.get("/api/v1/me", headers=auth_header(session))).status_code == 401
    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
    )
    assert resp.status_code == 401
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "new.staff@example.com", "password": PASSWORD}
    )
    assert resp.json()["error"]["code"] == "account_disabled"

    actions = set((await db.scalars(select(AuditLog.action))).all())
    assert {"staff.create", "staff.update", "staff.deactivate", "staff.resend_invite"} <= actions


async def test_customers_list_detail_and_deactivate(client: AsyncClient, h):
    await _completed_inquiry(client, h)
    listing = (
        await client.get(f"{ADMIN}/customers", params={"q": "perera"}, headers=h["admin"])
    ).json()
    assert listing["total"] == 1
    customer = listing["items"][0]
    assert customer["inquiries_count"] == 1

    detail = (await client.get(f"{ADMIN}/customers/{customer['id']}", headers=h["admin"])).json()
    assert detail["inquiries"][0]["status"] == "completed"
    assert detail["applications"] == []

    resp = await client.post(
        f"{ADMIN}/customers/{customer['id']}/active", json={"is_active": False}, headers=h["admin"]
    )
    assert resp.json()["is_active"] is False
    assert (await client.get("/api/v1/me", headers=h["cust"])).status_code == 401
    # Staff ids are not customers.
    assert (
        await client.get(f"{ADMIN}/customers/{customer['id']}", headers=h["s1"])
    ).status_code == 403


async def test_reviews_flow_and_moderation(client: AsyncClient, h):
    inquiry = await _completed_inquiry(client, h)

    # Can't review an open inquiry or someone else's.
    open_inquiry = (
        await client.post(
            "/api/v1/inquiries",
            json={"subject": "Another one", "description": "Another question here."},
            headers=h["cust"],
        )
    ).json()
    resp = await client.post(
        "/api/v1/reviews", json={"inquiry_id": open_inquiry["id"], "rating": 5}, headers=h["cust"]
    )
    assert resp.json()["error"]["code"] == "not_completed"

    resp = await client.post(
        "/api/v1/reviews",
        json={"inquiry_id": inquiry["id"], "rating": 5, "comment": "Very helpful team!"},
        headers=h["cust"],
    )
    assert resp.status_code == 201, resp.text
    review = resp.json()
    assert review["staff"]["full_name"] == "Staff One"
    resp = await client.post(
        "/api/v1/reviews", json={"inquiry_id": inquiry["id"], "rating": 4}, headers=h["cust"]
    )
    assert resp.status_code == 409
    resp = await client.post(
        "/api/v1/reviews", json={"inquiry_id": inquiry["id"], "rating": 6}, headers=h["cust"]
    )
    assert resp.status_code == 422

    mine = (
        await client.get("/api/v1/reviews", params={"inquiry_id": inquiry["id"]}, headers=h["cust"])
    ).json()
    assert mine["total"] == 1
    public = (await client.get("/api/v1/reviews/public")).json()
    assert public["average"] == 5.0
    assert public["reviews"][0]["author"] == "Jane P."

    # Admin replies (customer emailed) and hides it; hidden reviews leave the public list.
    resp = await client.patch(
        f"{ADMIN}/reviews/{review['id']}",
        json={"admin_reply": "Thank you, Jane!", "is_visible": False},
        headers=h["admin"],
    )
    assert resp.json()["admin_reply"] == "Thank you, Jane!"
    assert resp.json()["is_visible"] is False
    assert last_email("cust@example.com")["context"]["message"] == "Thank you, Jane!"
    assert (await client.get("/api/v1/reviews/public")).json()["count"] == 0
    # Staff only see visible reviews about them.
    assert (await client.get("/api/v1/reviews", headers=h["s1"])).json()["total"] == 0

    flagged = (
        await client.get(f"{ADMIN}/reviews", params={"flagged": True}, headers=h["admin"])
    ).json()
    assert flagged["total"] == 0
    await client.patch(
        f"{ADMIN}/reviews/{review['id']}", json={"flagged": True}, headers=h["admin"]
    )
    flagged = (
        await client.get(f"{ADMIN}/reviews", params={"flagged": True}, headers=h["admin"])
    ).json()
    assert flagged["total"] == 1


async def test_dashboard_counts(client: AsyncClient, h):
    await _completed_inquiry(client, h)
    await client.post(
        "/api/v1/inquiries",
        json={"subject": "Second", "description": "Unassigned question."},
        headers=h["cust"],
    )
    data = (await client.get(f"{ADMIN}/dashboard", headers=h["admin"])).json()
    assert data["customers_total"] == 1
    assert data["staff_active"] == 1
    assert data["inquiries_by_status"]["completed"] == 1
    assert data["inquiries_unassigned"] == 1
    assert len(data["inquiries_per_week"]) == 8
    assert data["inquiries_per_week"][-1]["count"] == 2
    assert data["recent_activity"][0]["actor_name"] == "Staff One"
    assert (await client.get(f"{ADMIN}/dashboard", headers=h["s1"])).status_code == 403


async def test_visa_type_management(client: AsyncClient, db: AsyncSession, h):
    resp = await client.post(
        f"{ADMIN}/visa-types",
        json={"name": "Working Holiday", "icon": "flight"},
        headers=h["admin"],
    )
    assert resp.status_code == 201, resp.text
    visa = resp.json()
    assert visa["slug"] == "working-holiday"

    resp = await client.post(
        f"{ADMIN}/visa-types/{visa['id']}/requirements",
        json={"name": "Passport"},
        headers=h["admin"],
    )
    resp = await client.post(
        f"{ADMIN}/visa-types/{visa['id']}/requirements",
        json={"name": "Bank statement", "is_mandatory": False},
        headers=h["admin"],
    )
    reqs = resp.json()["requirements"]
    assert [(r["name"], r["sort_order"]) for r in reqs] == [("Passport", 0), ("Bank statement", 1)]

    resp = await client.patch(
        f"/api/v1/admin/requirements/{reqs[1]['id']}",
        json={"is_mandatory": True},
        headers=h["admin"],
    )
    assert all(r["is_mandatory"] for r in resp.json()["requirements"])
    resp = await client.delete(f"/api/v1/admin/requirements/{reqs[0]['id']}", headers=h["admin"])
    assert [r["name"] for r in resp.json()["requirements"]] == ["Bank statement"]

    # Inactive types disappear from the public list but stay in admin.
    await client.patch(
        f"{ADMIN}/visa-types/{visa['id']}", json={"is_active": False}, headers=h["admin"]
    )
    public = (await client.get("/api/v1/visa-types")).json()
    assert visa["id"] not in [v["id"] for v in public]
    admin_list = (await client.get(f"{ADMIN}/visa-types", headers=h["admin"])).json()
    assert visa["id"] in [v["id"] for v in admin_list]
    resp = await client.post(
        f"{ADMIN}/visa-types", json={"name": "Working Holiday"}, headers=h["admin"]
    )
    assert resp.status_code == 409
    assert await db.scalar(select(VisaType.id).where(VisaType.slug == "working-holiday"))
    assert (await db.scalars(select(DocumentRequirement))).all()
