import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompanyInfo, DocumentRequirement, UserRole, VisaType
from tests.conftest import auth_header, create_user, last_email, login

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
async def visa(db: AsyncSession) -> VisaType:
    visa = VisaType(name="Student Visa", slug="student", icon="school", sort_order=0)
    visa.requirements = [
        DocumentRequirement(name="Passport", is_mandatory=True, sort_order=0),
        DocumentRequirement(name="Offer letter", is_mandatory=True, sort_order=1),
        DocumentRequirement(name="Statement of purpose", is_mandatory=False, sort_order=2),
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
    await create_user(db, email="staff@example.com", role=UserRole.STAFF, team_id=visa.id)
    return auth_header(await login(client, "staff@example.com"))


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
    assert [r["name"] for r in resp.json()[0]["requirements"]] == [
        "Passport",
        "Offer letter",
        "Statement of purpose",
    ]
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


async def test_application_document_flow(client: AsyncClient, visa, customer, staff):
    app = await _new_app(client, customer, visa)
    assert app["status"] == "draft"
    assert app["progress"] == {"required": 2, "uploaded": 0, "approved": 0}
    passport, offer, _ = (item["requirement"]["id"] for item in app["checklist"])

    # Cannot submit with mandatory documents missing.
    resp = await client.post(f"/api/v1/applications/{app['id']}/submit", headers=customer)
    assert resp.json()["error"]["code"] == "documents_missing"

    resp = await _upload(client, customer, app["id"], passport)
    assert resp.status_code == 201, resp.text
    assert resp.json()["mime_type"] == "application/pdf"
    passport_doc = resp.json()["id"]
    resp = await _upload(client, customer, app["id"], offer, data=PNG, name="offer.png")
    assert resp.json()["mime_type"] == "image/png"
    offer_doc = resp.json()["id"]

    resp = await client.post(f"/api/v1/applications/{app['id']}/submit", headers=customer)
    assert resp.status_code == 200
    assert resp.json()["status"] == "submitted"

    # Staff rejects without a reason -> validation error; with a reason -> ok + email.
    url = f"/api/v1/documents/{offer_doc}/review"
    resp = await client.post(url, json={"status": "rejected"}, headers=staff)
    assert resp.status_code == 422
    resp = await client.post(
        url, json={"status": "rejected", "rejection_reason": "Blurry scan"}, headers=staff
    )
    assert resp.status_code == 200
    assert resp.json()["rejection_reason"] == "Blurry scan"
    assert last_email("cust@example.com")["context"]["reason"] == "Blurry scan"

    detail = (await client.get(f"/api/v1/applications/{app['id']}", headers=customer)).json()
    assert detail["status"] == "in_review"
    assert detail["checklist"][1]["document"]["status"] == "rejected"
    assert detail["progress"]["uploaded"] == 1

    # Customer re-uploads; staff approves everything; then approves the application.
    resp = await _upload(client, customer, app["id"], offer)
    offer_doc = resp.json()["id"]
    for doc in (passport_doc, offer_doc):
        resp = await client.post(
            f"/api/v1/documents/{doc}/review", json={"status": "approved"}, headers=staff
        )
        assert resp.status_code == 200
    resp = await client.post(
        f"/api/v1/applications/{app['id']}/status", json={"status": "approved"}, headers=staff
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert resp.json()["progress"]["approved"] == 2

    # Closed applications can't take new uploads.
    resp = await _upload(client, customer, app["id"], passport)
    assert resp.json()["error"]["code"] == "application_closed"


async def test_upload_rejects_bad_files(client: AsyncClient, visa, customer, monkeypatch):
    app = await _new_app(client, customer, visa)
    resp = await _upload(client, customer, app["id"], data=b"MZ\x90\x00 not a pdf", name="x.pdf")
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_file_type"

    from app.core.config import settings

    monkeypatch.setattr(settings, "max_upload_bytes", 20)
    resp = await _upload(client, customer, app["id"], data=PDF)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"


async def test_download_url_and_access_control(client: AsyncClient, db, visa, customer, staff):
    app = await _new_app(client, customer, visa)
    doc = (await _upload(client, customer, app["id"])).json()

    resp = await client.get(f"/api/v1/documents/{doc['id']}/download-url", headers=staff)
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
    resp = await client.get(f"/api/v1/documents/{doc['id']}/download-url", headers=other)
    assert resp.status_code == 404
    listing = (await client.get("/api/v1/applications", headers=other)).json()
    assert listing["total"] == 0

    # Staff can't upload or create applications on behalf of customers.
    assert (await _upload(client, staff, app["id"])).status_code == 403


async def test_delete_pending_document_and_withdraw(client: AsyncClient, visa, customer):
    app = await _new_app(client, customer, visa)
    doc = (await _upload(client, customer, app["id"])).json()
    resp = await client.delete(f"/api/v1/documents/{doc['id']}", headers=customer)
    assert resp.status_code == 204
    detail = (await client.get(f"/api/v1/applications/{app['id']}", headers=customer)).json()
    assert detail["other_documents"] == []

    resp = await client.post(f"/api/v1/applications/{app['id']}/withdraw", headers=customer)
    assert resp.json()["status"] == "withdrawn"
    resp = await client.post(f"/api/v1/applications/{app['id']}/withdraw", headers=customer)
    assert resp.status_code == 400


async def test_invalid_status_transition(client: AsyncClient, visa, customer, staff):
    app = await _new_app(client, customer, visa)
    resp = await client.post(
        f"/api/v1/applications/{app['id']}/status", json={"status": "approved"}, headers=staff
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_transition"
