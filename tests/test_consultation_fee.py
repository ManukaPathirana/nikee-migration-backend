"""Consultation fee paid outside the app: set by the team admin, receipt from the customer,
approved by the team admin before the meeting can be booked."""

from httpx import AsyncClient

from app.models import CompanyInfo, UserRole
from app.services import email_service
from tests.conftest import auth_header, create_user, login
from tests.test_meetings import _new_inquiry, _slot

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


async def _people(client: AsyncClient, db) -> dict:
    await create_user(db, email="cust@example.com", full_name="Cust Omer")
    await create_user(db, email="other@example.com", full_name="Other Person")
    await create_user(db, email="s1@example.com", role=UserRole.STAFF, full_name="Staff One")
    await create_user(db, email="lead@example.com", role=UserRole.ADMIN, full_name="Team Lead")
    await create_user(db, email="admin@example.com", role=UserRole.SUPER_ADMIN, full_name="Boss")
    return {
        k: auth_header(await login(client, f"{k}@example.com"))
        for k in ("cust", "other", "s1", "lead", "admin")
    }


async def test_fee_flow_gates_booking(client: AsyncClient, db):
    h = await _people(client, db)
    iid = await _new_inquiry(client, h["cust"])
    base = f"/api/v1/inquiries/{iid}"
    book = {
        "inquiry_id": iid,
        "mode": "online",
        "meeting_link": "https://meet.example.com/x",
        "slots": [_slot()],
    }

    detail = (await client.get(base, headers=h["cust"])).json()
    assert detail["fee_status"] == "none"
    r = await client.post("/api/v1/meetings", json=book, headers=h["lead"])
    assert r.status_code == 400 and r.json()["error"]["code"] == "fee_not_approved"

    # Only the team admin sets the fee; the customer can't upload before it's set.
    r = await client.post(f"{base}/fee-receipt", files={"file": ("r.png", PNG)}, headers=h["cust"])
    assert r.json()["error"]["code"] == "no_fee"
    r = await client.post(f"{base}/fee", json={"amount": "150"}, headers=h["s1"])
    assert r.status_code == 403
    email_service.outbox.clear()
    r = await client.post(f"{base}/fee", json={"amount": "150.50"}, headers=h["lead"])
    assert r.status_code == 200, r.text
    assert r.json()["fee_status"] == "requested" and r.json()["fee_cents"] == 15050
    assert r.json()["fee_currency"] == "AUD"
    assert any("cust@example.com" in m["to"] for m in email_service.outbox)

    # Wrong file type, other customers, then a real receipt.
    r = await client.post(
        f"{base}/fee-receipt", files={"file": ("r.txt", b"hello")}, headers=h["cust"]
    )
    assert r.status_code == 415
    r = await client.post(f"{base}/fee-receipt", files={"file": ("r.png", PNG)}, headers=h["other"])
    assert r.status_code in (403, 404)
    r = await client.post(f"{base}/fee-receipt", files={"file": ("r.png", PNG)}, headers=h["cust"])
    assert r.status_code == 200 and r.json()["fee_status"] == "receipt_uploaded"
    assert r.json()["fee_receipt_name"] == "r.png"
    for who in ("cust", "lead"):
        r = await client.get(f"{base}/fee-receipt/download-url", headers=h[who])
        assert r.status_code == 200 and r.json()["url"]
    notes = (await client.get("/api/v1/notifications", headers=h["lead"])).json()["items"]
    assert any(n["type"] == "inquiry_fee_receipt" for n in notes)

    # Reject needs a reason; the customer uploads again; approve; then booking works.
    r = await client.post(f"{base}/fee/review", json={"approve": False}, headers=h["lead"])
    assert r.json()["error"]["code"] == "reason_required"
    r = await client.post(
        f"{base}/fee/review",
        json={"approve": False, "reason": "Amount is wrong"},
        headers=h["lead"],
    )
    assert r.json()["fee_status"] == "rejected"
    assert r.json()["fee_rejection_reason"] == "Amount is wrong"
    r = await client.post("/api/v1/meetings", json=book, headers=h["lead"])
    assert r.json()["error"]["code"] == "fee_not_approved"
    r = await client.post(f"{base}/fee-receipt", files={"file": ("r2.png", PNG)}, headers=h["cust"])
    assert r.json()["fee_status"] == "receipt_uploaded" and r.json()["fee_rejection_reason"] is None
    r = await client.post(f"{base}/fee/review", json={"approve": True}, headers=h["lead"])
    assert r.json()["fee_status"] == "approved" and r.json()["fee_approved_at"]
    r = await client.post(f"{base}/fee", json={"amount": "99"}, headers=h["lead"])
    assert r.json()["error"]["code"] == "fee_approved"
    r = await client.post("/api/v1/meetings", json=book, headers=h["lead"])
    assert r.status_code == 201, r.text


async def test_general_inquiries_have_no_fee(client: AsyncClient, db):
    h = await _people(client, db)
    iid = await _new_inquiry(client, h["cust"], kind="general")
    r = await client.post(f"/api/v1/inquiries/{iid}/fee", json={"amount": "50"}, headers=h["lead"])
    assert r.json()["error"]["code"] == "not_consultation"


async def test_company_bank_details(client: AsyncClient, db):
    h = await _people(client, db)
    db.add(CompanyInfo(name="Nikee Migrations"))
    await db.commit()
    body = {"bank_details": ""}
    body["bank_details"] = "Bank: ANZ\nBSB: 012-345\nAccount: 1234 5678"
    r = await client.put("/api/v1/admin/company", json=body, headers=h["admin"])
    assert r.status_code == 200, r.text
    assert (await client.get("/api/v1/company")).json()["bank_details"].startswith("Bank: ANZ")
    body["bank_details"] = ""
    r = await client.put("/api/v1/admin/company", json=body, headers=h["admin"])
    assert r.json()["bank_details"] is None


async def test_company_google_review_link(client: AsyncClient, db):
    h = await _people(client, db)
    db.add(CompanyInfo(name="Nikee Migrations"))
    await db.commit()
    url = "https://g.page/r/CabcDEF123/review"
    r = await client.put(
        "/api/v1/admin/company", json={"google_review_url": url}, headers=h["admin"]
    )
    assert r.status_code == 200, r.text
    assert (await client.get("/api/v1/company")).json()["google_review_url"] == url
    r = await client.put(
        "/api/v1/admin/company", json={"google_review_url": "not a link"}, headers=h["admin"]
    )
    assert r.status_code == 422
    r = await client.put(
        "/api/v1/admin/company", json={"google_review_url": ""}, headers=h["admin"]
    )
    assert r.json()["google_review_url"] is None
