"""The full visa case journey: intake -> assignment -> contract + deposit -> documents ->
final invoice -> data confirmation -> completed. Payments use the built-in test checkout."""

import base64
import io

import pytest
from httpx import AsyncClient
from PIL import Image, ImageDraw
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocumentRequirement, UserRole, VisaType
from tests.conftest import auth_header, create_user, login

API = "/api/v1"


def make_pdf(text: str = "Service contract") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def signature_b64() -> str:
    img = Image.new("RGBA", (300, 100), (255, 255, 255, 0))
    ImageDraw.Draw(img).line([(10, 80), (120, 20), (290, 70)], fill=(11, 42, 74, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
async def ctx(client: AsyncClient, db: AsyncSession) -> dict:
    visa = VisaType(name="Student", slug="student", icon="school", sort_order=0)
    visa.requirements = [
        DocumentRequirement(name="Passport", is_mandatory=True, is_intake=True, sort_order=0),
        DocumentRequirement(name="Visa grant", is_mandatory=True, is_intake=True, sort_order=1),
        DocumentRequirement(name="Offer letter", is_mandatory=True, sort_order=2),
    ]
    db.add(visa)
    await db.commit()
    staff = await create_user(db, email="staff@example.com", role=UserRole.STAFF, team_id=visa.id)
    await create_user(db, email="other@example.com", role=UserRole.STAFF, team_id=visa.id)
    await create_user(db, email="lead@example.com", role=UserRole.ADMIN, team_id=visa.id)
    await create_user(db, email="cust@example.com", full_name="Cust Omer")
    await create_user(db, email="boss@example.com", role=UserRole.SUPER_ADMIN)
    h = {
        k: auth_header(await login(client, f"{k}@example.com"))
        for k in ("staff", "other", "lead", "cust", "boss")
    }
    return {"visa": visa, "staff_id": str(staff.id), **h}


async def _case(client: AsyncClient, ctx: dict) -> dict:
    resp = await client.post(
        f"{API}/applications",
        json={"visa_type_id": str(ctx["visa"].id), "destination_country": "Australia"},
        headers=ctx["cust"],
    )
    app = resp.json()
    # Before the deposit is approved the customer only sees the intake documents.
    assert [i["requirement"]["name"] for i in app["checklist"]] == ["Passport", "Visa grant"]
    ids = {r.name: str(r.id) for r in ctx["visa"].requirements}
    for name in ("Passport", "Visa grant"):
        await client.post(
            f"{API}/applications/{app['id']}/documents",
            files={"file": (f"{name}.pdf", make_pdf(name), "application/pdf")},
            data={"requirement_id": ids[name]},
            headers=ctx["cust"],
        )
    resp = await client.post(f"{API}/applications/{app['id']}/submit", headers=ctx["cust"])
    assert resp.status_code == 200, resp.text
    return app | {"req": ids}


async def _pay(
    client: AsyncClient, ctx: dict, invoice_id: str, payment_type: str | None = None
) -> None:
    resp = await client.post(
        f"{API}/invoices/{invoice_id}/checkout",
        json={"payment_type": payment_type} if payment_type else None,
        headers=ctx["cust"],
    )
    assert resp.status_code == 200, resp.text
    path = resp.json()["url"].split("http://localhost:8000", 1)[-1]
    page = await client.get(path)
    assert page.status_code == 200 and "Pay A$" in page.text
    done = await client.post(path)
    assert "Payment received" in done.text


async def _download(client: AsyncClient, headers: dict, url: str) -> bytes:
    link = (await client.get(url, headers=headers)).json()["url"]
    resp = await client.get(link)
    assert resp.status_code == 200
    return resp.content


async def test_full_visa_case_journey(client: AsyncClient, ctx):
    app = await _case(client, ctx)
    case = f"{API}/applications/{app['id']}"

    # Team admin assigns a staff member of the team.
    resp = await client.post(
        f"{case}/assign", json={"staff_id": ctx["staff_id"]}, headers=ctx["lead"]
    )
    assert resp.json()["stage"] == "contract_pending"
    assert resp.json()["assigned_staff"]["email"] == "staff@example.com"
    # The customer sees the team admin, never the staff member handling the case.
    mine = (await client.get(case, headers=ctx["cust"])).json()["assigned_staff"]
    assert mine["email"] == "lead@example.com"
    note = (await client.get(f"{API}/notifications", headers=ctx["cust"])).json()["items"][0]
    assert "staff@example.com" not in str(note)
    # Only that staff member sees it.
    assert (await client.get(case, headers=ctx["other"])).status_code == 404

    overview = (await client.get(f"{API}/admin/team-overview", headers=ctx["lead"])).json()
    row = next(r for r in overview["staff"] if r["email"] == "staff@example.com")
    assert row["cases_total"] == 1 and row["by_phase"]["new"] == 1
    assert row["waiting"][0]["task"] == "Send contract and deposit invoice"
    assert overview["waiting_on_staff"] == 1

    # Staff sends the contract (PDF only) and the deposit.
    bad = await client.post(
        f"{case}/agreement",
        files={"contract": ("c.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40, "image/png")},
        data={"deposit": "1800"},
        headers=ctx["staff"],
    )
    assert bad.json()["error"]["code"] == "pdf_required"
    bad = await client.post(
        f"{case}/agreement",
        files={"contract": ("c.pdf", make_pdf(), "application/pdf")},
        data={"deposit": "abc"},
        headers=ctx["staff"],
    )
    assert bad.json()["error"]["code"] == "invalid_amount"
    resp = await client.post(
        f"{case}/agreement",
        files={"contract": ("contract.pdf", make_pdf(), "application/pdf")},
        data={"deposit": "1,800", "payment_type": "  Half payment of   visa application fee "},
        headers=ctx["staff"],
    )
    detail = resp.json()
    assert detail["stage"] == "agreement" and detail["waiting_on"] == "customer"
    deposit = detail["invoices"][0]
    assert deposit["kind"] == "deposit" and deposit["amount_cents"] == 180000
    assert deposit["status"] == "unpaid" and deposit["number"].startswith("NM-INV-")
    invoice_pdf = await _download(
        client, ctx["cust"], f"{API}/invoices/{deposit['id']}/download-url"
    )
    assert invoice_pdf.startswith(b"%PDF")
    assert deposit["payment_type"] == "Half payment of visa application fee"
    assert deposit["description"].startswith("Half payment of visa application fee")
    text = PdfReader(io.BytesIO(invoice_pdf)).pages[0].extract_text()
    assert "Payment type: Half payment of visa application fee" in text

    # Still locked: the rest of the checklist.
    locked = await client.post(
        f"{case}/documents",
        files={"file": ("offer.pdf", make_pdf(), "application/pdf")},
        data={"requirement_id": app["req"]["Offer letter"]},
        headers=ctx["cust"],
    )
    assert locked.json()["error"]["code"] == "documents_locked"

    # Customer signs: the signed copy has a signature page added.
    resp = await client.post(
        f"{case}/sign",
        json={"kind": "contract", "signer_name": "Cust Omer", "signature_png": signature_b64()},
        headers=ctx["cust"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["stage"] == "agreement"  # deposit not paid yet
    signed = next(f for f in resp.json()["files"] if f["kind"] == "signed_contract")
    pdf = await _download(client, ctx["staff"], f"{API}/case-files/{signed['id']}/download-url")
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 2

    # Deposit paid through the test checkout -> staff reviews.
    await _pay(client, ctx, deposit["id"])
    boss = (await client.get(f"{API}/notifications", headers=ctx["boss"])).json()["items"]
    assert {"case_new", "payment_received"} <= {n["type"] for n in boss}
    detail = (await client.get(case, headers=ctx["cust"])).json()
    assert detail["stage"] == "agreement_review"
    assert detail["invoices"][0]["status"] == "paid"

    # Staff recalls the contract; customer signs again; staff approves -> documents unlock.
    resp = await client.post(
        f"{case}/agreement/recall",
        json={"reason": "Please use your full legal name"},
        headers=ctx["staff"],
    )
    assert resp.json()["stage"] == "agreement"
    resp = await client.post(
        f"{case}/sign",
        json={"kind": "contract", "signer_name": "Cust A. Omer", "signature_png": signature_b64()},
        headers=ctx["cust"],
    )
    assert resp.json()["stage"] == "agreement_review"
    resp = await client.post(f"{case}/agreement/approve", headers=ctx["staff"])
    # Now the customer sees the whole checklist.
    names = [
        i["requirement"]["name"]
        for i in (await client.get(case, headers=ctx["cust"])).json()["checklist"]
    ]
    assert "Offer letter" in names
    detail = resp.json()
    assert detail["stage"] == "documents" and detail["documents_unlocked"] is True
    assert all(not i["locked"] for i in detail["checklist"])

    # Customer uploads; staff can't finalize until every document is approved.
    resp = await client.post(
        f"{case}/documents",
        files={"file": ("offer.pdf", make_pdf("Offer"), "application/pdf")},
        data={"requirement_id": app["req"]["Offer letter"]},
        headers=ctx["cust"],
    )
    assert resp.status_code == 201
    assert (await client.get(case, headers=ctx["staff"])).json()["waiting_on"] == "staff"
    inbox = (await client.get(f"{API}/notifications", headers=ctx["staff"])).json()["items"]
    assert any(n["type"] == "document_uploaded" for n in inbox)  # staff hear about uploads
    resp = await client.post(f"{case}/finalize", json={"amount": "1800"}, headers=ctx["staff"])
    assert resp.json()["error"]["code"] == "documents_not_approved"
    for item in (await client.get(case, headers=ctx["staff"])).json()["checklist"]:
        r = await client.post(
            f"{API}/documents/{item['document']['id']}/review",
            json={"status": "approved"},
            headers=ctx["staff"],
        )
        assert r.status_code == 200, r.text
    resp = await client.post(
        f"{case}/finalize",
        json={"amount": "1800", "payment_type": "Full payment of visa application fee"},
        headers=ctx["staff"],
    )
    detail = resp.json()
    assert detail["stage"] == "final_payment"
    final = next(i for i in detail["invoices"] if i["kind"] == "final")
    assert final["payment_type"] == "Full payment of visa application fee"  # chosen by staff

    # Staff chose the payment type; the customer can't change it when paying.
    resp = await client.post(
        f"{API}/invoices/{final['id']}/checkout",
        json={"payment_type": "Other"},
        headers=ctx["cust"],
    )
    assert resp.status_code == 200
    await _pay(client, ctx, final["id"])
    final = next(
        i
        for i in (await client.get(case, headers=ctx["cust"])).json()["invoices"]
        if i["kind"] == "final"
    )
    assert final["payment_type"] == "Full payment of visa application fee"
    assert final["description"].startswith("Full payment of visa application fee")
    final_pdf = await _download(client, ctx["cust"], f"{API}/invoices/{final['id']}/download-url")
    text = PdfReader(io.BytesIO(final_pdf)).pages[0].extract_text()
    assert "Payment type: Full payment of visa application fee" in text and "PAID" in text
    assert (await client.get(case, headers=ctx["cust"])).json()["stage"] == "confirmation_pending"

    # Final application + data confirmation; customer signs; staff completes.
    resp = await client.post(
        f"{case}/confirmation",
        files={
            "final_application": ("final.pdf", make_pdf("Final"), "application/pdf"),
            "data_confirmation": ("confirm.pdf", make_pdf("Confirm"), "application/pdf"),
        },
        headers=ctx["staff"],
    )
    assert resp.json()["stage"] == "confirmation"

    # The customer finds a mistake and sends both papers back with the reason.
    resp = await client.post(
        f"{case}/confirmation/request-changes",
        json={"reason": "My date of birth is wrong"},
        headers=ctx["staff"],
    )
    assert resp.status_code == 403
    resp = await client.post(
        f"{case}/confirmation/request-changes",
        json={"reason": "My date of birth is wrong"},
        headers=ctx["cust"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["stage"] == "confirmation_pending"
    papers = ("final_application", "data_confirmation")
    recalled = [f for f in resp.json()["files"] if f["recalled_at"] and f["kind"] in papers]
    assert {f["kind"] for f in recalled} == set(papers)
    assert all(f["recall_reason"] == "My date of birth is wrong" for f in recalled)
    staff_inbox = (await client.get(f"{API}/notifications", headers=ctx["staff"])).json()["items"]
    assert "asked for changes" in staff_inbox[0]["title"]
    # Staff fixes them and sends new ones.
    resp = await client.post(
        f"{case}/confirmation",
        files={
            "final_application": ("final2.pdf", make_pdf("Final v2"), "application/pdf"),
            "data_confirmation": ("confirm2.pdf", make_pdf("Confirm v2"), "application/pdf"),
        },
        headers=ctx["staff"],
    )
    assert resp.json()["stage"] == "confirmation"
    resp = await client.post(
        f"{case}/sign",
        json={
            "kind": "data_confirmation",
            "signer_name": "Cust A. Omer",
            "signature_png": signature_b64(),
        },
        headers=ctx["cust"],
    )
    assert resp.json()["stage"] == "confirmation_review"
    resp = await client.post(f"{case}/complete", headers=ctx["staff"])
    assert resp.json()["stage"] == "completed"
    assert resp.json()["status"] == "approved"

    overview = (await client.get(f"{API}/admin/team-overview", headers=ctx["lead"])).json()
    assert overview["open_cases"] == 0
    assert overview["paid_this_week_cents"] == 360000


async def test_only_the_case_owner_acts(client: AsyncClient, ctx):
    app = await _case(client, ctx)
    case = f"{API}/applications/{app['id']}"
    # Staff can't assign; wrong stage is refused; the other staff member can't act.
    r = await client.post(
        f"{case}/assign", json={"staff_id": ctx["staff_id"]}, headers=ctx["staff"]
    )
    assert r.status_code == 403
    await client.post(f"{case}/assign", json={"staff_id": ctx["staff_id"]}, headers=ctx["lead"])
    r = await client.post(f"{case}/agreement/approve", headers=ctx["staff"])
    assert r.json()["error"]["code"] == "wrong_stage"
    r = await client.post(f"{case}/agreement/approve", headers=ctx["other"])
    assert r.status_code == 404
    # Customers can't sign before there's a contract.
    r = await client.post(
        f"{case}/sign",
        json={"kind": "contract", "signer_name": "Cust Omer", "signature_png": signature_b64()},
        headers=ctx["cust"],
    )
    assert r.json()["error"]["code"] == "wrong_stage"


async def test_payment_gateway_settings(client: AsyncClient, ctx, monkeypatch):
    from app.core.config import settings

    app = await _case(client, ctx)
    case = f"{API}/applications/{app['id']}"
    await client.post(f"{case}/assign", json={"staff_id": ctx["staff_id"]}, headers=ctx["lead"])
    detail = (
        await client.post(
            f"{case}/agreement",
            files={"contract": ("c.pdf", make_pdf(), "application/pdf")},
            data={"deposit": "500"},
            headers=ctx["staff"],
        )
    ).json()
    inv = detail["invoices"][0]["id"]

    # Stripe chosen but no key: a clear error, nothing charged.
    monkeypatch.setattr(settings, "payments_backend", "stripe")
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    r = await client.post(f"{API}/invoices/{inv}/checkout", headers=ctx["cust"])
    assert r.status_code == 503 and r.json()["error"]["code"] == "payments_unavailable"
    # Test payments never run in production.
    monkeypatch.setattr(settings, "payments_backend", "test")
    monkeypatch.setattr(settings, "app_env", "production")
    r = await client.post(f"{API}/invoices/{inv}/checkout", headers=ctx["cust"])
    assert r.status_code == 503
    # Only the customer pays.
    monkeypatch.setattr(settings, "app_env", "test")
    r = await client.post(f"{API}/invoices/{inv}/checkout", headers=ctx["staff"])
    assert r.status_code == 403


async def test_team_admin_starts_a_case_for_existing_and_new_customers(
    client: AsyncClient, db: AsyncSession, ctx
):
    from app.services import email_service
    from tests.conftest import last_email

    # Find the customer.
    found = (
        await client.get(f"{API}/admin/customer-search", params={"q": "cust"}, headers=ctx["lead"])
    ).json()
    assert [c["email"] for c in found] == ["cust@example.com"]
    cust_id = found[0]["id"]
    resp = await client.get(
        f"{API}/admin/customer-search", params={"q": "cust"}, headers=ctx["staff"]
    )
    assert resp.status_code == 403

    # Existing customer: the case waits for their first step.
    resp = await client.post(
        f"{API}/admin/cases",
        json={"customer_id": cust_id, "note": "Please send these this week"},
        headers=ctx["lead"],
    )
    assert resp.status_code == 201, resp.text
    case = resp.json()
    assert case["stage"] == "intake" and case["waiting_on"] == "customer"
    assert case["customer"]["email"] == "cust@example.com"
    note = (await client.get(f"{API}/notifications", headers=ctx["cust"])).json()["items"][0]
    assert note["title"] == "Your Student visa case has started"
    mail = last_email("cust@example.com")
    assert mail["context"]["link"].endswith(f"/#/customer/applications/{case['id']}")
    assert mail["context"]["reason"] == "Please send these this week"
    # The customer sees it and can do the first step.
    mine = (await client.get(f"{API}/applications", headers=ctx["cust"])).json()["items"]
    assert [a["id"] for a in mine] == [case["id"]]
    # Only one open case per visa type.
    again = await client.post(
        f"{API}/admin/cases", json={"customer_id": cust_id}, headers=ctx["lead"]
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "case_exists"

    # New customer by email: they get an email to sign up, and see the case after signing up.
    email_service.outbox.clear()
    resp = await client.post(
        f"{API}/admin/cases",
        json={"email": "New.Person@example.com", "full_name": "New Person", "phone": "0771234567"},
        headers=ctx["lead"],
    )
    assert resp.status_code == 201, resp.text
    new_case = resp.json()
    invite = last_email("new.person@example.com")
    assert invite["context"]["link"].endswith("/#/register")
    reg = await client.post(
        f"{API}/auth/register",
        json={"email": "new.person@example.com", "password": "Sup3rSecret!", "full_name": "New P"},
    )
    assert reg.status_code == 201, reg.text
    code = last_email("new.person@example.com")["context"]["code"]
    tokens = (
        await client.post(
            f"{API}/auth/verify-email", json={"email": "new.person@example.com", "code": code}
        )
    ).json()
    newbie = {"Authorization": f"Bearer {tokens.get('tokens', tokens)['access_token']}"}
    mine = (await client.get(f"{API}/applications", headers=newbie)).json()["items"]
    assert [a["id"] for a in mine] == [new_case["id"]]

    # Staff can't start cases; a team admin can't for another team; staff emails are refused.
    resp = await client.post(
        f"{API}/admin/cases", json={"customer_id": cust_id}, headers=ctx["staff"]
    )
    assert resp.status_code == 403
    other_team = VisaType(name="Appeal", slug="appeal", sort_order=1)
    db.add(other_team)
    await db.commit()
    resp = await client.post(
        f"{API}/admin/cases",
        json={"customer_id": cust_id, "visa_type_id": str(other_team.id)},
        headers=ctx["lead"],
    )
    assert resp.status_code == 403
    resp = await client.post(
        f"{API}/admin/cases",
        json={"email": "staff@example.com", "full_name": "Staff"},
        headers=ctx["lead"],
    )
    assert resp.json()["error"]["code"] == "not_a_customer"


async def test_request_extra_document_for_one_case(client: AsyncClient, db: AsyncSession, ctx):
    app = await _case(client, ctx)
    case = f"{API}/applications/{app['id']}"
    body = {"name": "Bank statement (6 months)", "description": "From your main account"}

    # Only at the documents step.
    resp = await client.post(f"{case}/requested-documents", json=body, headers=ctx["lead"])
    assert resp.json()["error"]["code"] == "wrong_stage"
    from app.models import VisaApplication

    row = await db.get(VisaApplication, app["id"])
    row.stage = "documents"
    await db.commit()

    # Only the case's handler (team admin here; nobody assigned) - not the customer or others.
    for who in ("cust", "other"):
        resp = await client.post(f"{case}/requested-documents", json=body, headers=ctx[who])
        assert resp.status_code in (403, 404), resp.text
    resp = await client.post(f"{case}/requested-documents", json=body, headers=ctx["lead"])
    assert resp.status_code == 200, resp.text
    names = [i["requirement"]["name"] for i in resp.json()["checklist"]]
    assert names == ["Passport", "Visa grant", "Offer letter", "Bank statement (6 months)"]
    extra = resp.json()["checklist"][-1]["requirement"]
    assert extra["application_id"] == app["id"] and extra["is_mandatory"] is True
    resp = await client.post(f"{case}/requested-documents", json=body, headers=ctx["lead"])
    assert resp.json()["error"]["code"] == "already_in_checklist"

    # The customer sees it, is told, and can upload it.
    detail = (await client.get(case, headers=ctx["cust"])).json()
    assert detail["checklist"][-1]["requirement"]["name"] == "Bank statement (6 months)"
    assert detail["progress"]["required"] == 4
    notes = (await client.get(f"{API}/notifications", headers=ctx["cust"])).json()["items"]
    assert any("Bank statement" in n["title"] for n in notes)
    up = await client.post(
        f"{case}/documents",
        files={"file": ("bank.pdf", make_pdf("bank"), "application/pdf")},
        data={"requirement_id": extra["id"]},
        headers=ctx["cust"],
    )
    assert up.status_code == 201, up.text

    # Not part of the visa type's own list, nor of other cases.
    vt = (await client.get(f"{API}/visa-types/student")).json()
    assert "Bank statement (6 months)" not in [r["name"] for r in vt["requirements"]]

    # Finalize waits for it; it can be taken back (the upload stays under other documents).
    resp = await client.post(f"{case}/finalize", json={"amount": "1800"}, headers=ctx["lead"])
    assert resp.json()["error"]["code"] == "documents_not_approved"
    resp = await client.delete(f"{case}/requested-documents/{extra['id']}", headers=ctx["lead"])
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["checklist"]) == 3
    assert [d["file_name"] for d in resp.json()["other_documents"]] == ["bank.pdf"]
