"""Render and deliver emails. Call only from background jobs (see job_queue.send_email)."""

import asyncio
import base64
import logging
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any, TypedDict

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import settings
from app.core.security import utcnow

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates" / "email"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape(["html"]))


class Attachment(TypedDict):
    filename: str
    content: bytes
    mime_type: str  # e.g. "text/calendar"


# Captured messages when EMAIL_BACKEND=memory (tests).
outbox: list[dict[str, Any]] = []


def render(template: str, context: dict[str, Any]) -> str:
    base_context = {
        "app_name": "Nikee Migrations",
        "website_url": "https://nikeemigration.com",
        "year": datetime.now().year,
    }
    return _env.get_template(template).render(**base_context, **context)


def _html_to_text(html: str) -> str:
    text = re.sub(r"<(br|/p|/h\d|/tr|/div)[^>]*>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def build_message(
    to: list[str], subject: str, html: str, attachments: list[Attachment] | None = None
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="nikeemigration.com")
    msg.set_content(_html_to_text(html))
    msg.add_alternative(html, subtype="html")
    for att in attachments or []:
        maintype, subtype = att["mime_type"].split("/", 1)
        msg.add_attachment(
            att["content"], maintype=maintype, subtype=subtype, filename=att["filename"]
        )
    return msg


def _smtp_send(msg: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


async def _resend_send(
    to: list[str], subject: str, html: str, attachments: list[Attachment] | None
) -> None:
    payload: dict[str, Any] = {
        "from": settings.email_from,
        "to": to,
        "subject": subject,
        "html": html,
    }
    if attachments:
        payload["attachments"] = [
            {"filename": a["filename"], "content": base64.b64encode(a["content"]).decode()}
            for a in attachments
        ]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        )
        resp.raise_for_status()


async def send_email(
    to: str | list[str],
    subject: str,
    html: str,
    attachments: list[Attachment] | None = None,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    recipients = [to] if isinstance(to, str) else list(to)
    backend = settings.email_backend

    if backend == "memory":
        outbox.append(
            {
                "to": recipients,
                "subject": subject,
                "html": html,
                "context": context or {},
                "attachments": attachments or [],
            }
        )
        return

    if backend == "console":
        logger.info("EMAIL to=%s subject=%s\n%s", recipients, subject, _html_to_text(html))
        return

    msg = build_message(recipients, subject, html, attachments)

    if backend == "file":
        out_dir = settings.resolve_path(settings.dev_mail_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:40]
        path = out_dir / f"{utcnow():%Y%m%d-%H%M%S-%f}_{slug}.eml"
        path.write_bytes(bytes(msg))
        logger.info("EMAIL to=%s subject=%s saved to %s", recipients, subject, path)
        return

    if backend == "smtp":
        await asyncio.to_thread(_smtp_send, msg)
        return

    if backend == "resend":
        await _resend_send(recipients, subject, html, attachments)
        return

    raise RuntimeError(f"Unknown EMAIL_BACKEND {backend!r}")
