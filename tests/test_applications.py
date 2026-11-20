import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompanyInfo, DocumentRequirement, UserRole, VisaType
from tests.conftest import auth_header, create_user, login

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
async def visa(db: AsyncSession) -> VisaType:
    visa = VisaType(name="Student Visa", slug="student", icon="school", sort_order=0)
    visa.requirements = [
        DocumentRequirement(name="Passport", is_mandatory=True, is_intake=True, sort_order=0),
        DocumentRequirement(name="Visa grant", is_mandatory=True, is_intake=True, sort_order=1),
        DocumentRequirement(name="Offer letter", is_mandatory=True, sort_order=2),
        DocumentRequirement(name="Statement of purpose", is_mandatory=False, sort_order=3),
    ]
    db.add(visa)
    await db.commit()
    return visa


@pytest.fixture
async def customer(client: AsyncClient, db: AsyncSession) -> dict:
    await create_user(db, email="cust@example.com", full_name="Cust Omer")
    return auth_header(await login(client, "cust@example.com"))


@pytest.fixture
async def staff(client: AsyncClient, db: AsyncSession, visa: VisaType) -> dict:
    user = await create_user(db, email="staff@example.com", role=UserRole.STAFF, team_id=visa.id)
    return auth_header(await login(client, "staff@example.com")) | {"x-id": str(user.id)}


@pytest.fixture
async def team_admin(client: AsyncClient, db: AsyncSession, visa: VisaType) -> dict:
    await create_user(db, email="lead@example.com", role=UserRole.ADMIN, team_id=visa.id)
    return auth_header(await login(client, "lead@example.com"))


async def _submitted(client, customer, visa) -> tuple[dict, dict]:
    """A case with passport + visa grant uploaded and submitted to the team admin."""
    app = await _new_app(client, customer, visa)
    ids = {i["requirement"]["name"]: i["requirement"]["id"] for i in app["checklist"]}
    docs = {}
    for name in ("Passport", "Visa grant"):
        resp = await _upload(client, customer, app["id"], ids[name])
        assert resp.status_code == 201, resp.text
        docs[name] = resp.json()["id"]
    resp = await client.post(f"/api/v1/applications/{app['id']}/submit", headers=customer)
    assert resp.status_code == 200, resp.text
    return resp.json() | {"req": ids}, docs


async def _new_app(client: AsyncClient, headers: dict, visa: VisaType) -> dict:
    resp = await client.post(
        "/api/v1/applications",
        json={"visa_type_id": str(visa.id), "destination_country": "Australia"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _upload(client, headers, app_id, requirement_id=None, data=PDF, name="passport.pdf"):
    form = {"requirement_id": requirement_id} if requirement_id else {}
    return await client.post(
        f"/api/v1/applications/{app_id}/documents",
        files={"file": (name, data, "application/octet-stream")},
        data=form,
        headers=headers,
    )


async def test_public_company_and_visa_types(client: AsyncClient, db: AsyncSession, visa):
    db.add(CompanyInfo(name="Nikee Migrations", about="About us"))
    await db.commit()
    resp = await client.get("/api/v1/company")
    assert resp.status_code == 200
    assert resp.json()["website_url"] == "https://nikeemigration.com"

    resp = await client.get("/api/v1/visa-types")
    # Public pages list only the documents needed to start.
    assert [r["name"] for r in resp.json()[0]["requirements"]] == ["Passport", "Visa grant"]
    assert (await client.get("/api/v1/visa-types/student")).status_code == 200
    assert (await client.get(f"/api/v1/visa-types/{visa.id}")).status_code == 200


async def test_admin_updates_company_others_cannot(client: AsyncClient, db: AsyncSession, customer):
    db.add(CompanyInfo(name="Nikee Migrations"))
    await create_user(db, email="admin@example.com", role=UserRole.SUPER_ADMIN)
    admin = auth_header(await login(client, "admin@example.com"))

    body = {"phone": "+94 77 123 4567", "whatsapp": "+94771234567", "email": "info@example.com"}
    assert (
        await client.put("/api/v1/admin/company", json=body, headers=customer)
    ).status_code == 403
    resp = await client.put("/api/v1/admin/company", json=body, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["whatsapp"] == "+94771234567"


async def test_intake_unlocks_only_passport_and_visa_grant(
    client: AsyncClient, db: AsyncSession, visa, customer, staff, team_admin
):
    app = await _new_app(client, customer, visa)
    assert app["status"] == "draft"
    assert app["stage"] == "intake"
    assert app["ref_no"].startswith("NM-C-")
    # The customer sees only the intake documents until the deposit is approved.
    locked = {i["requirement"]["name"]: i["locked"] for i in app["checklist"]}
    assert locked == {"Passport": False, "Visa grant": False}
    assert app["progress"]["required"] == 2
    ids = {r.name: str(r.id) for r in visa.requirements}

    # Can't submit without both intake documents; can't upload locked ones.
    resp = await client.post(f"/api/v1/applications/{app['id']}/submit", headers=customer)
    assert resp.json()["error"]["code"] == "documents_missing"
    resp = await _upload(client, customer, app["id"], ids["Offer letter"])
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "documents_locked"
    resp = await _upload(client, customer, app["id"])
    assert resp.json()["error"]["code"] == "documents_locked"

    resp = await _upload(client, customer, app["id"], ids["Passport"], data=PNG, name="p.png")
    assert resp.json()["mime_type"] == "image/png"
    await _upload(client, customer, app["id"], ids["Visa grant"])
    resp = await client.post(f"/api/v1/applications/{app['id']}/submit", headers=customer)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stage"] == "assignment"
    assert body["waiting_on"] == "admin"
    assert body["status"] == "submitted"

    # The team admin is told; staff can't see a case that isn't theirs.
    notes = (await client.get("/api/v1/notifications", headers=team_admin)).json()["items"]
    assert any(n["type"] == "case_new" for n in notes)
    assert (await client.get(f"/api/v1/applications/{app['id']}", headers=staff)).status_code == 404
    assert (await client.get("/api/v1/applications", headers=staff)).json()["total"] == 0
    waiting = await client.get("/api/v1/applications?waiting=admin", headers=team_admin)
    assert waiting.json()["total"] == 1


async def test_upload_rejects_bad_files(client: AsyncClient, visa, customer, monkeypatch):
    app = await _new_app(client, customer, visa)
    passport = app["checklist"][0]["requirement"]["id"]
    resp = await _upload(client, customer, app["id"], passport, data=b"MZ not a pdf", name="x.pdf")
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_file_type"

    from app.core.config import settings

    monkeypatch.setattr(settings, "max_upload_bytes", 20)
    resp = await _upload(client, customer, app["id"], passport, data=PDF)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"


async def test_upload_reports_unwritable_storage(client: AsyncClient, visa, customer, monkeypatch):
    """A read-only disk (e.g. Vercel without STORAGE_BACKEND=s3) gives a clear 503, not a 500."""
    from pathlib import Path

    app = await _new_app(client, customer, visa)
    passport = app["checklist"][0]["requirement"]["id"]

    def read_only(*_args, **_kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "write_bytes", read_only)
    resp = await _upload(client, customer, app["id"], passport)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "storage_unavailable"
    detail = await client.get(f"/api/v1/applications/{app['id']}", headers=customer)
    assert detail.json()["checklist"][0]["document"] is None  # nothing half-saved


async def test_download_url_and_access_control(
    client: AsyncClient, db, visa, customer, staff, team_admin
):
    app, docs = await _submitted(client, customer, visa)
    resp = await client.post(
        f"/api/v1/applications/{app['id']}/assign",
        json={"staff_id": staff["x-id"]},
        headers=team_admin,
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"/api/v1/documents/{docs['Passport']}/download-url", headers=staff)
    assert resp.status_code == 200
    url = resp.json()["url"]
    assert url.startswith("/api/v1/files/")
    file_resp = await client.get(url)
    assert file_resp.status_code == 200
    assert file_resp.content == PDF
    assert file_resp.headers["content-type"] == "application/pdf"
    assert (await client.get("/api/v1/files/not-a-token")).status_code == 403

    # Another customer can't see the application or the document.
    await create_user(db, email="other@example.com")
    other = auth_header(await login(client, "other@example.com"))
    assert (await client.get(f"/api/v1/applications/{app['id']}", headers=other)).status_code == 404
    resp = await client.get(f"/api/v1/documents/{docs['Passport']}/download-url", headers=other)
    assert resp.status_code == 404
    listing = (await client.get("/api/v1/applications", headers=other)).json()
    assert listing["total"] == 0

    # Staff can't upload on behalf of customers.
    assert (await _upload(client, staff, app["id"], app["req"]["Passport"])).status_code == 403


async def test_delete_pending_document_and_withdraw(client: AsyncClient, visa, customer):
    app = await _new_app(client, customer, visa)
    passport = app["checklist"][0]["requirement"]["id"]
    doc = (await _upload(client, customer, app["id"], passport)).json()
    resp = await client.delete(f"/api/v1/documents/{doc['id']}", headers=customer)
    assert resp.status_code == 204
    detail = (await client.get(f"/api/v1/applications/{app['id']}", headers=customer)).json()
    assert detail["checklist"][0]["document"] is None

    resp = await client.post(f"/api/v1/applications/{app['id']}/withdraw", headers=customer)
    assert resp.json()["status"] == "withdrawn"
    assert resp.json()["stage"] == "withdrawn"
    resp = await client.post(f"/api/v1/applications/{app['id']}/withdraw", headers=customer)
    assert resp.status_code == 400


async def test_old_status_endpoint_only_rejects(client: AsyncClient, visa, customer, team_admin):
    app, _ = await _submitted(client, customer, visa)
    url = f"/api/v1/applications/{app['id']}/status"
    resp = await client.post(url, json={"status": "approved"}, headers=team_admin)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "use_case_steps"
    resp = await client.post(url, json={"status": "rejected"}, headers=team_admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stage"] == "withdrawn"


async def test_upload_and_download_via_vercel_blob(
    client: AsyncClient, visa, customer, monkeypatch
):
    """With BLOB_READ_WRITE_TOKEN set, files go to a private Vercel Blob store."""
    import httpx

    from app.core.config import settings
    from app.services import storage_service

    blobs: dict[str, bytes] = {}
    calls: list[httpx.Request] = []

    def fake_blob_api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer vercel_blob_rw_store123_secret"
        if request.method == "PUT":
            assert request.headers["x-vercel-blob-access"] == "private"
            blobs[request.url.params["pathname"]] = request.content
            return httpx.Response(200, json={"url": "x"})
        if request.url.host == "store123.private.blob.vercel-storage.com":
            key = request.url.path.lstrip("/")
            return httpx.Response(200, content=blobs[key]) if key in blobs else httpx.Response(404)
        return httpx.Response(404)

    monkeypatch.setattr(settings, "blob_read_write_token", "vercel_blob_rw_store123_secret")
    storage_service.get_storage.cache_clear()
    try:
        storage = storage_service.get_storage()
        assert isinstance(storage, storage_service.BlobStorage)
        storage.transport = httpx.MockTransport(fake_blob_api)

        app = await _new_app(client, customer, visa)
        passport = app["checklist"][0]["requirement"]["id"]
        resp = await _upload(client, customer, app["id"], passport)
        assert resp.status_code == 201, resp.text
        assert list(blobs.values()) == [PDF]

        doc_id = resp.json()["id"]
        url = (
            await client.get(f"/api/v1/documents/{doc_id}/download-url", headers=customer)
        ).json()["url"]
        file = await client.get(url)
        assert file.status_code == 200
        assert file.content == PDF
        assert file.headers["content-type"] == "application/pdf"
        assert file.headers["content-disposition"].startswith("inline;")
    finally:
        storage_service.get_storage.cache_clear()
