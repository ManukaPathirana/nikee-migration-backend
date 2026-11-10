from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserRole
from app.services import auth_service, email_service
from tests.conftest import PASSWORD, auth_header, create_user, last_email, login

API = "/api/v1/auth"


async def _register(client: AsyncClient, email: str = "new@example.com", **extra) -> None:
    body = {"email": email, "password": PASSWORD, "full_name": "New Customer", **extra}
    resp = await client.post(f"{API}/register", json=body)
    assert resp.status_code == 201, resp.text


async def test_register_verify_login_flow(client: AsyncClient):
    await _register(client, "Jane@Example.com")
    mail = last_email("jane@example.com")
    code = mail["context"]["code"]
    assert len(code) == 6 and code.isdigit()

    # Unverified users cannot sign in.
    resp = await client.post(
        f"{API}/login", json={"email": "jane@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "email_not_verified"

    wrong = "000000" if code != "000000" else "111111"
    resp = await client.post(
        f"{API}/verify-email", json={"email": "jane@example.com", "code": wrong}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_code"

    resp = await client.post(
        f"{API}/verify-email", json={"email": "JANE@example.com", "code": code}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["is_verified"] is True
    assert body["user"]["role"] == "customer"
    assert body["user"]["email"] == "jane@example.com"

    me = await client.get("/api/v1/me", headers=auth_header(body))
    assert me.status_code == 200
    assert me.json()["customer_profile"] is not None

    tokens = await login(client, "jane@example.com")
    assert tokens["expires_in"] == 15 * 60


async def test_code_cannot_be_reused(client: AsyncClient):
    await _register(client)
    code = last_email("new@example.com")["context"]["code"]
    resp = await client.post(f"{API}/verify-email", json={"email": "new@example.com", "code": code})
    assert resp.status_code == 200
    resp = await client.post(f"{API}/verify-email", json={"email": "new@example.com", "code": code})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "already_verified"


async def test_register_existing_verified_email_conflicts(client: AsyncClient, db: AsyncSession):
    await create_user(db, email="taken@example.com")
    body = {"email": "TAKEN@example.com", "password": PASSWORD, "full_name": "Someone"}
    resp = await client.post(f"{API}/register", json=body)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "email_taken"


async def test_reregister_unverified_sends_new_code_and_invalidates_old(client: AsyncClient):
    await _register(client)
    old = last_email("new@example.com")["context"]["code"]
    await _register(client)
    new = last_email("new@example.com")["context"]["code"]
    if old != new:
        resp = await client.post(
            f"{API}/verify-email", json={"email": "new@example.com", "code": old}
        )
        assert resp.status_code == 400
    resp = await client.post(f"{API}/verify-email", json={"email": "new@example.com", "code": new})
    assert resp.status_code == 200


async def test_otp_locks_after_too_many_wrong_attempts(client: AsyncClient):
    await _register(client)
    code = last_email("new@example.com")["context"]["code"]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        resp = await client.post(
            f"{API}/verify-email", json={"email": "new@example.com", "code": wrong}
        )
        assert resp.json()["error"]["code"] == "invalid_code"
    resp = await client.post(f"{API}/verify-email", json={"email": "new@example.com", "code": code})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "code_locked"


async def test_weak_password_returns_validation_error_format(client: AsyncClient):
    body = {"email": "x@example.com", "password": "short", "full_name": "X"}
    resp = await client.post(f"{API}/register", json=body)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    fields = {d["field"] for d in error["details"]}
    assert {"password", "full_name"} <= fields


async def test_login_wrong_password_and_inactive(client: AsyncClient, db: AsyncSession):
    await create_user(db, email="a@example.com")
    await create_user(db, email="off@example.com", active=False)

    resp = await client.post(
        f"{API}/login", json={"email": "a@example.com", "password": "Nope1234"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credentials"

    resp = await client.post(f"{API}/login", json={"email": "nobody@example.com", "password": "x"})
    assert resp.status_code == 401

    resp = await client.post(
        f"{API}/login", json={"email": "off@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_disabled"


async def test_refresh_rotates_and_detects_reuse(client: AsyncClient, db: AsyncSession):
    await create_user(db, email="r@example.com")
    first = await login(client, "r@example.com")

    resp = await client.post(f"{API}/refresh", json={"refresh_token": first["refresh_token"]})
    assert resp.status_code == 200
    second = resp.json()
    assert second["refresh_token"] != first["refresh_token"]

    # Reusing the rotated token is treated as theft: it fails and kills the newer one too.
    resp = await client.post(f"{API}/refresh", json={"refresh_token": first["refresh_token"]})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_refresh_token"
    resp = await client.post(f"{API}/refresh", json={"refresh_token": second["refresh_token"]})
    assert resp.status_code == 401


async def test_logout_revokes_refresh_token(client: AsyncClient, db: AsyncSession):
    await create_user(db, email="l@example.com")
    tokens = await login(client, "l@example.com")
    resp = await client.post(f"{API}/logout", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 204
    resp = await client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 401


async def test_forgot_and_reset_password(client: AsyncClient, db: AsyncSession):
    await create_user(db, email="f@example.com")
    old_session = await login(client, "f@example.com")

    resp = await client.post(f"{API}/forgot-password", json={"email": "unknown@example.com"})
    assert resp.status_code == 200
    assert email_service.outbox == []

    resp = await client.post(f"{API}/forgot-password", json={"email": "f@example.com"})
    assert resp.status_code == 200
    code = last_email("f@example.com")["context"]["code"]

    body = {"email": "f@example.com", "code": code, "new_password": "BrandNew456"}
    resp = await client.post(f"{API}/reset-password", json=body)
    assert resp.status_code == 200, resp.text

    resp = await client.post(f"{API}/login", json={"email": "f@example.com", "password": PASSWORD})
    assert resp.status_code == 401
    await login(client, "f@example.com", "BrandNew456")

    # Existing sessions are signed out after a reset.
    resp = await client.post(f"{API}/refresh", json={"refresh_token": old_session["refresh_token"]})
    assert resp.status_code == 401


async def test_staff_accepts_invite(client: AsyncClient, db: AsyncSession):
    admin = await create_user(db, email="boss@example.com", role=UserRole.SUPER_ADMIN)
    staff = await create_user(
        db, email="staff@example.com", role=UserRole.STAFF, password=None, verified=False
    )
    await auth_service.issue_invite(db, staff, invited_by=admin)
    mail = last_email("staff@example.com")
    token = mail["context"]["token"]
    assert token in mail["context"]["link"]

    body = {"token": token, "password": "StaffPass1"}
    resp = await client.post(f"{API}/accept-invite", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["role"] == "staff"
    await login(client, "staff@example.com", "StaffPass1")

    # Invite links are single use.
    resp = await client.post(f"{API}/accept-invite", json=body)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_invite"


async def test_me_requires_auth_and_updates_profile(client: AsyncClient, db: AsyncSession):
    resp = await client.get("/api/v1/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "not_authenticated"

    resp = await client.get("/api/v1/me", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"

    await create_user(db, email="me@example.com")
    tokens = await login(client, "me@example.com")
    resp = await client.patch(
        "/api/v1/me",
        headers=auth_header(tokens),
        json={"full_name": "Updated Name", "customer_profile": {"nationality": "Sri Lankan"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["full_name"] == "Updated Name"
    assert resp.json()["customer_profile"]["nationality"] == "Sri Lankan"


async def test_change_password_signs_out_other_sessions(client: AsyncClient, db: AsyncSession):
    await create_user(db, email="c@example.com")
    tokens = await login(client, "c@example.com")
    body = {"current_password": PASSWORD, "new_password": "Changed789"}
    resp = await client.post(f"{API}/change-password", json=body, headers=auth_header(tokens))
    assert resp.status_code == 200
    resp = await client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 401
    await login(client, "c@example.com", "Changed789")


async def test_health(client: AsyncClient):
    resp = await client.get("/api/v1/health")
    assert resp.json() == {"status": "ok", "database": "ok"}
