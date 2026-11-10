"""Teams (visa types), team admins, the super admin and work hours."""

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utcnow
from app.models import ActivityEvent, UserRole, VisaType, WorkSession
from tests.conftest import TEAM, auth_header, create_user, login

API = "/api/v1"


@pytest.fixture
async def org(client: AsyncClient, db: AsyncSession) -> dict:
    """Two teams: the default one (student-like) and Appeal, each with an admin and staff."""
    appeal = VisaType(name="Appeal", slug="appeal", sort_order=1)
    db.add(appeal)
    await db.commit()
    people = {
        "super": await create_user(db, email="super@example.com", role=UserRole.SUPER_ADMIN),
        "adm_a": await create_user(db, email="adm.a@example.com", role=UserRole.ADMIN),
        "staff_a": await create_user(db, email="staff.a@example.com", role=UserRole.STAFF),
        "adm_b": await create_user(
            db, email="adm.b@example.com", role=UserRole.ADMIN, team_id=appeal.id
        ),
        "staff_b": await create_user(
            db, email="staff.b@example.com", role=UserRole.STAFF, team_id=appeal.id
        ),
        "cust": await create_user(db, email="cust@example.com"),
    }
    ctx: dict = {"appeal": str(appeal.id)}
    for key, user in people.items():
        ctx[key] = auth_header(await login(client, user.email))
        ctx[f"{key}_id"] = str(user.id)
    return ctx


async def _inquiry(client: AsyncClient, ctx: dict, team: str) -> dict:
    resp = await client.post(
        f"{API}/inquiries",
        json={"visa_type_id": team, "subject": "Help please", "description": "My visa question."},
        headers=ctx["cust"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------- routing


async def test_inquiry_goes_to_its_team_only(client: AsyncClient, org):
    inq = await _inquiry(client, org, org["appeal"])
    assert inq["visa_type"]["slug"] == "appeal"

    # The Appeal admin is notified and can open it; the other team can't see it at all.
    notes = (await client.get(f"{API}/notifications", headers=org["adm_b"])).json()["items"]
    assert any(n["type"] == "inquiry_new" for n in notes)
    assert (
        await client.get(f"{API}/inquiries/{inq['id']}", headers=org["adm_b"])
    ).status_code == 200
    assert (
        await client.get(f"{API}/inquiries/{inq['id']}", headers=org["adm_a"])
    ).status_code == 404
    assert (
        await client.get(f"{API}/inquiries/{inq['id']}", headers=org["staff_a"])
    ).status_code == 404

    board_a = (await client.get(f"{API}/admin/inquiries/board", headers=org["adm_a"])).json()
    assert sum(board_a["counts"].values()) == 0
    board_b = (await client.get(f"{API}/admin/inquiries/board", headers=org["adm_b"])).json()
    assert sum(board_b["counts"].values()) == 1
    # The super admin sees every team, and can filter to one.
    everyone = (await client.get(f"{API}/admin/inquiries/board", headers=org["super"])).json()
    assert sum(everyone["counts"].values()) == 1
    only_a = await client.get(
        f"{API}/admin/inquiries/board", params={"team_id": TEAM}, headers=org["super"]
    )
    assert sum(only_a.json()["counts"].values()) == 0


async def test_admin_assigns_only_own_team_staff(client: AsyncClient, org):
    inq = await _inquiry(client, org, org["appeal"])
    url = f"{API}/inquiries/{inq['id']}/assign"
    wrong = await client.post(url, json={"staff_id": org["staff_a_id"]}, headers=org["super"])
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "wrong_team"
    ok = await client.post(url, json={"staff_id": org["staff_b_id"]}, headers=org["adm_b"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["assigned_staff"]["id"] == org["staff_b_id"]

    # The staff picker only offers the admin's own team.
    picker = (await client.get(f"{API}/staff", headers=org["adm_b"])).json()
    assert {s["id"] for s in picker} == {org["staff_b_id"]}


# ---------------------------------------------------------------- managing people


async def test_team_admin_adds_staff_to_own_team_only(client: AsyncClient, org):
    url = f"{API}/admin/staff"
    resp = await client.post(
        url, json={"email": "new@example.com", "full_name": "New Person"}, headers=org["adm_b"]
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["team"]["slug"] == "appeal"
    assert resp.json()["role"] == "staff"

    other = await client.post(
        url,
        json={"email": "x@example.com", "full_name": "X Person", "team_id": TEAM},
        headers=org["adm_b"],
    )
    assert other.status_code == 403
    admin = await client.post(
        url,
        json={"email": "y@example.com", "full_name": "Y Person", "role": "admin"},
        headers=org["adm_b"],
    )
    assert admin.status_code == 403

    listed = (await client.get(url, headers=org["adm_b"])).json()["items"]
    assert {s["email"] for s in listed} == {"staff.b@example.com", "new@example.com"}
    # Other teams' staff are invisible to this admin.
    hidden = await client.patch(
        f"{url}/{org['staff_a_id']}", json={"full_name": "Hacked"}, headers=org["adm_b"]
    )
    assert hidden.status_code == 404


async def test_super_admin_creates_team_admin_and_moves_people(client: AsyncClient, org):
    url = f"{API}/admin/staff"
    resp = await client.post(
        url,
        json={
            "email": "lead@example.com",
            "full_name": "Team Lead",
            "role": "admin",
            "team_id": org["appeal"],
        },
        headers=org["super"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "admin"
    missing_team = await client.post(
        url, json={"email": "z@example.com", "full_name": "Z Person"}, headers=org["super"]
    )
    assert missing_team.json()["error"]["code"] == "team_required"

    everyone = (await client.get(url, headers=org["super"])).json()["items"]
    assert len(everyone) == 5  # 2 admins + 2 staff + the new lead
    moved = await client.patch(
        f"{url}/{org['staff_a_id']}", json={"team_id": org["appeal"]}, headers=org["super"]
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["team"]["slug"] == "appeal"
    # Team admins can't move people between teams.
    denied = await client.patch(
        f"{url}/{org['staff_b_id']}", json={"team_id": TEAM}, headers=org["adm_b"]
    )
    assert denied.status_code == 403


async def test_company_wide_settings_are_super_admin_only(client: AsyncClient, org):
    for method, path in [
        ("get", "/admin/customers"),
        ("get", "/admin/visa-types"),
        ("put", "/admin/company"),
    ]:
        kwargs = {"json": {"name": "X"}} if method == "put" else {}
        resp = await getattr(client, method)(f"{API}{path}", headers=org["adm_a"], **kwargs)
        assert resp.status_code == 403, path
    assert (await client.get(f"{API}/admin/customers", headers=org["super"])).status_code == 200


async def test_dashboards_by_role(client: AsyncClient, org):
    await _inquiry(client, org, org["appeal"])
    await _inquiry(client, org, TEAM)
    sup = (await client.get(f"{API}/admin/dashboard", headers=org["super"])).json()
    assert sup["scope"] == "all"
    assert sum(sup["inquiries_by_status"].values()) == 2
    teams = {t["team"]["slug"]: t for t in sup["teams"]}
    assert sum(teams["appeal"]["inquiries_by_status"].values()) == 1
    assert teams["appeal"]["admins"] == ["Test User"]

    team = (await client.get(f"{API}/admin/dashboard", headers=org["adm_b"])).json()
    assert team["scope"] == "team"
    assert team["team"]["slug"] == "appeal"
    assert team["teams"] == []
    assert sum(team["inquiries_by_status"].values()) == 1
    assert team["staff_active"] == 1


# ---------------------------------------------------------------- work hours


async def test_start_and_end_work(client: AsyncClient, org):
    me = org["staff_b"]
    assert (await client.get(f"{API}/me/work", headers=me)).json()["working"] is False
    first = (await client.post(f"{API}/me/work/start", headers=me)).json()
    again = (await client.post(f"{API}/me/work/start", headers=me)).json()
    assert first["working"] and again["current"]["id"] == first["current"]["id"]
    ended = (await client.post(f"{API}/me/work/end", headers=me)).json()
    assert ended["working"] is False
    # Customers have no clock.
    assert (await client.post(f"{API}/me/work/start", headers=org["cust"])).status_code == 403


async def test_sign_out_ends_shift_and_is_logged(client: AsyncClient, db: AsyncSession, org):
    tokens = await login(client, "staff.b@example.com")
    h = auth_header(tokens)
    await client.post(f"{API}/me/work/start", headers=h)
    resp = await client.post(f"{API}/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 204
    shift = await db.scalar(select(WorkSession).where(WorkSession.ended_at.is_not(None)))
    assert shift is not None and shift.end_reason == "logout"
    kinds = set(await db.scalars(select(ActivityEvent.kind)))
    assert kinds == {"login", "logout"}
    # Customers' sign-ins aren't tracked.
    cust_events = await db.scalar(
        select(ActivityEvent).where(ActivityEvent.user_id == org["cust_id"])
    )
    assert cust_events is None


async def test_report_shows_hours_and_respects_teams(client: AsyncClient, db: AsyncSession, org):
    now = utcnow()
    db.add(
        WorkSession(
            user_id=org["staff_b_id"],
            started_at=now - timedelta(hours=3),
            ended_at=now - timedelta(hours=1),
            end_reason="manual",
        )
    )
    await db.commit()
    params = {"from": (now - timedelta(days=1)).isoformat(), "to": now.isoformat()}

    report = await client.get(f"{API}/admin/reports/staff", params=params, headers=org["adm_b"])
    assert report.status_code == 200, report.text
    rows = {r["email"]: r for r in report.json()["rows"]}
    # A team admin sees their staff and themselves, not other teams or other admins.
    assert set(rows) == {"adm.b@example.com", "staff.b@example.com"}
    assert rows["staff.b@example.com"]["worked_seconds"] == pytest.approx(7200, abs=2)
    assert rows["staff.b@example.com"]["last_login_at"] is not None

    sup = await client.get(f"{API}/admin/reports/staff", params=params, headers=org["super"])
    assert len(sup.json()["rows"]) == 4

    detail = await client.get(f"{API}/admin/attendance/{org['staff_b_id']}", headers=org["adm_b"])
    assert detail.status_code == 200
    assert len(detail.json()["sessions"]) == 1
    other = await client.get(f"{API}/admin/attendance/{org['staff_a_id']}", headers=org["adm_b"])
    assert other.status_code == 404
