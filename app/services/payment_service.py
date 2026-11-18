"""Payment gateway: Stripe Checkout (AUD), or a built-in test checkout for development."""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from jose import JWTError, jwt

from app.core.config import settings
from app.core.errors import AppError
from app.core.security import utcnow


class PaymentsUnavailable(AppError):
    status_code = 503
    code = "payments_unavailable"


@dataclass
class CheckoutResult:
    url: str
    checkout_id: str


def _return_url(invoice_id: uuid.UUID) -> str:
    return f"{settings.public_api_url}{settings.api_v1_prefix}/payments/return?invoice={invoice_id}"


def backend() -> str:
    if settings.payments_backend == "stripe":
        if not settings.stripe_secret_key:
            raise PaymentsUnavailable(
                "Payments aren't set up yet. Add STRIPE_SECRET_KEY to the server settings."
            )
        return "stripe"
    if settings.is_production:
        raise PaymentsUnavailable("Test payments are switched off in production")
    return "test"


def _stripe() -> Any:
    import stripe

    stripe.api_key = settings.stripe_secret_key
    return stripe


# ---------------------------------------------------------------- test checkout


def test_token(invoice_id: uuid.UUID) -> str:
    payload = {
        "type": "test_pay",
        "inv": str(invoice_id),
        "exp": int((utcnow() + timedelta(hours=2)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_test_token(token: str) -> uuid.UUID | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("type") != "test_pay":
        return None
    return uuid.UUID(payload["inv"])


# ---------------------------------------------------------------- checkout


async def create_checkout(
    *,
    invoice_id: uuid.UUID,
    number: str,
    description: str,
    amount_cents: int,
    currency: str,
    customer_email: str,
    title: str | None = None,
) -> CheckoutResult:
    if backend() == "test":
        token = test_token(invoice_id)
        return CheckoutResult(
            url=f"{settings.public_api_url}{settings.api_v1_prefix}/payments/test-checkout/{token}",
            checkout_id=f"test_{uuid.uuid4().hex[:16]}",
        )
    stripe = _stripe()
    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        mode="payment",
        customer_email=customer_email,
        client_reference_id=str(invoice_id),
        metadata={"invoice_id": str(invoice_id), "invoice_number": number},
        line_items=[
            {
                "quantity": 1,
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": amount_cents,
                    "product_data": {
                        "name": f"{title} - Invoice {number}" if title else f"Invoice {number}",
                        "description": description,
                    },
                },
            }
        ],
        success_url=_return_url(invoice_id) + "&session_id={CHECKOUT_SESSION_ID}",
        cancel_url=_return_url(invoice_id) + "&cancelled=1",
    )
    return CheckoutResult(url=session.url, checkout_id=session.id)


async def check_paid(checkout_id: str) -> str | None:
    """Payment reference if the checkout has been paid, else None."""
    if checkout_id.startswith("test_"):
        return None  # test payments are confirmed by the test checkout page itself
    stripe = _stripe()
    session = await asyncio.to_thread(stripe.checkout.Session.retrieve, checkout_id)
    if session.payment_status == "paid":
        return str(session.payment_intent or session.id)
    return None


def parse_webhook(payload: bytes, signature: str) -> tuple[str, str] | None:
    """(checkout_id, payment_ref) for a completed, paid checkout; None for other events."""
    stripe = _stripe()
    event = stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)
    if event["type"] not in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    ):
        return None
    session = event["data"]["object"]
    if session.get("payment_status") != "paid":
        return None
    return session["id"], str(session.get("payment_intent") or session["id"])
