"""Visa case steps, case files, invoices and payment pages."""

import html
import uuid

from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.core.deps import AdminUser, CurrentUser, CustomerUser, DbSession, StaffOrAdmin
from app.core.errors import BadRequest, NotFound
from app.models import CaseFileKind, Invoice, InvoiceStatus
from app.schemas.visa import (
    AmountIn,
    ApplicationDetail,
    AssignIn,
    ChangesIn,
    CheckoutOut,
    DownloadUrlOut,
    ReasonIn,
    SignIn,
)
from app.services import application_service as apps
from app.services import case_service as cases
from app.services import payment_service
from app.services.pdf_service import money

router = APIRouter(tags=["cases"])


def _device(request: Request) -> str | None:
    return request.headers.get("user-agent")


# ---------------------------------------------------------------- steps


@router.post("/applications/{app_id}/assign", response_model=ApplicationDetail)
async def assign_case(
    app_id: uuid.UUID, data: AssignIn, admin: AdminUser, db: DbSession
) -> ApplicationDetail:
    """Team admin: give the case to one staff member of its team."""
    app = await apps.get_application(db, app_id, admin)
    return apps.to_detail(await cases.assign(db, app, admin, data.staff_id))


@router.post("/applications/{app_id}/agreement", response_model=ApplicationDetail)
async def send_agreement(
    app_id: uuid.UUID,
    staff: StaffOrAdmin,
    db: DbSession,
    contract: UploadFile = File(...),
    deposit: str = Form(..., max_length=20),
    payment_type: str = Form("", max_length=120),
) -> ApplicationDetail:
    """Staff: upload the contract PDF and set the deposit; the app creates the invoice."""
    app = await apps.get_application(db, app_id, staff)
    return apps.to_detail(
        await cases.send_agreement(
            db, app, staff, contract=contract, deposit=deposit, payment_type=payment_type
        )
    )


@router.post("/applications/{app_id}/sign", response_model=ApplicationDetail)
async def sign(
    app_id: uuid.UUID, data: SignIn, request: Request, user: CustomerUser, db: DbSession
) -> ApplicationDetail:
    """Customer: sign the contract or the data confirmation with a drawn signature."""
    app = await apps.get_application(db, app_id, user)
    return apps.to_detail(
        await cases.sign(
            db,
            app,
            user,
            kind=CaseFileKind(data.kind),
            signer_name=data.signer_name,
            signature_png_b64=data.signature_png,
            device=_device(request),
        )
    )


@router.post("/applications/{app_id}/agreement/approve", response_model=ApplicationDetail)
async def approve_agreement(
    app_id: uuid.UUID, staff: StaffOrAdmin, db: DbSession
) -> ApplicationDetail:
    """Staff: signed contract and deposit are correct; unlock the full checklist."""
    app = await apps.get_application(db, app_id, staff)
    return apps.to_detail(await cases.approve_agreement(db, app, staff))


@router.post("/applications/{app_id}/agreement/recall", response_model=ApplicationDetail)
async def recall_agreement(
    app_id: uuid.UUID, data: ReasonIn, staff: StaffOrAdmin, db: DbSession
) -> ApplicationDetail:
    """Staff: the signed contract isn't right; the customer signs again."""
    app = await apps.get_application(db, app_id, staff)
    return apps.to_detail(await cases.recall_agreement(db, app, staff, data.reason))


@router.post("/applications/{app_id}/finalize", response_model=ApplicationDetail)
async def finalize(
    app_id: uuid.UUID, data: AmountIn, staff: StaffOrAdmin, db: DbSession
) -> ApplicationDetail:
    """Staff: every required document is approved; send the final invoice."""
    app = await apps.get_application(db, app_id, staff)
    return apps.to_detail(await cases.finalize(db, app, staff, data.amount, data.payment_type))


@router.post("/applications/{app_id}/confirmation", response_model=ApplicationDetail)
async def send_confirmation(
    app_id: uuid.UUID,
    staff: StaffOrAdmin,
    db: DbSession,
    final_application: UploadFile = File(...),
    data_confirmation: UploadFile = File(...),
) -> ApplicationDetail:
    """Staff: send the finalized application and the data confirmation PDF to sign."""
    app = await apps.get_application(db, app_id, staff)
    return apps.to_detail(
        await cases.send_confirmation(
            db,
            app,
            staff,
            final_application=final_application,
            data_confirmation=data_confirmation,
        )
    )


@router.post(
    "/applications/{app_id}/confirmation/request-changes", response_model=ApplicationDetail
)
async def request_confirmation_changes(
    app_id: uuid.UUID, data: ChangesIn, user: CustomerUser, db: DbSession
) -> ApplicationDetail:
    """Customer: send the finalized application / data confirmation back with what to fix."""
    app = await apps.get_application(db, app_id, user)
    return apps.to_detail(await cases.request_confirmation_changes(db, app, user, data.reason))


@router.post("/applications/{app_id}/confirmation/recall", response_model=ApplicationDetail)
async def recall_confirmation(
    app_id: uuid.UUID, data: ReasonIn, staff: StaffOrAdmin, db: DbSession
) -> ApplicationDetail:
    app = await apps.get_application(db, app_id, staff)
    return apps.to_detail(await cases.recall_confirmation(db, app, staff, data.reason))


@router.post("/applications/{app_id}/complete", response_model=ApplicationDetail)
async def complete(app_id: uuid.UUID, staff: StaffOrAdmin, db: DbSession) -> ApplicationDetail:
    app = await apps.get_application(db, app_id, staff)
    return apps.to_detail(await cases.complete(db, app, staff))


# ---------------------------------------------------------------- files and invoices


@router.get("/case-files/{file_id}/download-url", response_model=DownloadUrlOut)
async def case_file_url(file_id: uuid.UUID, user: CurrentUser, db: DbSession) -> DownloadUrlOut:
    url, _ = await cases.file_url(db, file_id, user)
    return DownloadUrlOut(url=url, expires_in=settings.signed_url_expire_seconds)


@router.get("/invoices/{invoice_id}/download-url", response_model=DownloadUrlOut)
async def invoice_file_url(
    invoice_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> DownloadUrlOut:
    url = await cases.invoice_url(db, invoice_id, user)
    return DownloadUrlOut(url=url, expires_in=settings.signed_url_expire_seconds)


@router.post("/invoices/{invoice_id}/checkout", response_model=CheckoutOut)
async def checkout(invoice_id: uuid.UUID, user: CustomerUser, db: DbSession) -> CheckoutOut:
    """Customer: start paying. Open the returned URL; it brings them back to the app.
    The payment type is set by staff or the team admin, not by the customer."""
    inv, app = await cases.get_invoice(db, invoice_id, user)
    return CheckoutOut(url=await cases.checkout(db, inv, app, user))


@router.post("/invoices/{invoice_id}/confirm", response_model=ApplicationDetail)
async def confirm(invoice_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ApplicationDetail:
    """Re-checks the payment with the gateway (the app calls this when the customer returns)."""
    inv, app = await cases.get_invoice(db, invoice_id, user)
    await cases.confirm_payment(db, inv)
    return apps.to_detail(await apps.get_application(db, app.id, user))


# ---------------------------------------------------------------- payment pages


def _page(title: str, body: str, *, ok: bool = True) -> HTMLResponse:
    color = "#16A34A" if ok else "#B45309"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#F7F8FA;
margin:0;padding:24px;color:#1A1F2B}}
.card{{max-width:420px;margin:40px auto;background:#fff;border:1px solid #E5E7EB;
border-radius:16px;padding:24px}}
h1{{font-size:22px;margin:0 0 8px;color:{color}}} p{{line-height:1.5;color:#545D6B}}
button{{width:100%;padding:14px;border:0;border-radius:12px;background:#0B2A4A;color:#fff;
font-size:16px;font-weight:600}}
.brand{{font-weight:700;color:#0B2A4A}} .brand span{{color:#C9A227}}
</style></head><body><div class="card">
<div class="brand">Nikee <span>Migrations</span></div><h1>{html.escape(title)}</h1>{body}
</div></body></html>""")


@router.get("/payments/test-checkout/{token}", include_in_schema=False)
async def test_checkout_page(token: str, db: DbSession) -> HTMLResponse:
    invoice_id = payment_service.verify_test_token(token)
    if invoice_id is None or settings.is_production:
        raise NotFound("Not found")
    inv = await db.get(Invoice, invoice_id)
    if inv is None:
        raise NotFound("Invoice not found")
    if inv.status == InvoiceStatus.PAID.value:
        return _page("Already paid", "<p>This invoice is paid. Go back to the app.</p>")
    amount = html.escape(money(inv.amount_cents, inv.currency))
    return _page(
        f"Pay {amount}",
        f"""<p>Invoice {html.escape(inv.number)}<br>{html.escape(inv.description)}</p>
<p><strong>Test payment</strong>: no card is charged. With Stripe switched on, customers
see the real Stripe checkout here.</p>
<form method="post"><button type="submit">Pay {amount} (test)</button></form>""",
        ok=True,
    )


@router.post("/payments/test-checkout/{token}", include_in_schema=False)
async def test_checkout_pay(token: str, db: DbSession) -> HTMLResponse:
    invoice_id = payment_service.verify_test_token(token)
    if invoice_id is None or settings.is_production:
        raise NotFound("Not found")
    inv = await db.get(Invoice, invoice_id)
    if inv is None:
        raise NotFound("Invoice not found")
    await cases.mark_paid(db, inv, f"TEST-{uuid.uuid4().hex[:10].upper()}")
    return _page(
        "Payment received", "<p>Thank you. You can close this page and go back to the app.</p>"
    )


@router.get("/payments/return", include_in_schema=False)
async def payment_return(
    invoice: uuid.UUID, db: DbSession, cancelled: int = 0, session_id: str | None = None
) -> HTMLResponse:
    """Stripe sends customers here after checkout."""
    inv = await db.get(Invoice, invoice)
    if inv is None:
        raise NotFound("Invoice not found")
    if cancelled:
        return _page(
            "Payment cancelled",
            "<p>No money was taken. Go back to the app to try again.</p>",
            ok=False,
        )
    if session_id and inv.checkout_id and session_id != inv.checkout_id:
        raise BadRequest("This payment link doesn't match the invoice", code="checkout_mismatch")
    paid = await cases.confirm_payment(db, inv)
    if paid:
        return _page("Payment received", "<p>Thank you. Go back to the app to continue.</p>")
    return _page(
        "Payment processing",
        "<p>We're confirming your payment. Go back to the app; it will update shortly.</p>",
        ok=False,
    )


@router.post("/payments/stripe/webhook", include_in_schema=False)
async def stripe_webhook(
    request: Request, db: DbSession, stripe_signature: str = Header(alias="Stripe-Signature")
) -> dict:
    if settings.payments_backend != "stripe" or not settings.stripe_webhook_secret:
        raise NotFound("Not found")
    try:
        parsed = payment_service.parse_webhook(await request.body(), stripe_signature)
    except Exception as e:  # bad signature or payload
        raise BadRequest("Invalid webhook", code="invalid_webhook") from e
    if parsed is not None:
        checkout_id, ref = parsed
        from sqlalchemy import select

        inv = await db.scalar(select(Invoice).where(Invoice.checkout_id == checkout_id))
        if inv is not None:
            await cases.mark_paid(db, inv, ref)
    return {"received": True}
