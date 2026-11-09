from typing import Any

from app.services import email_service
from app.services.email_service import Attachment


async def send_email_job(
    ctx: dict[str, Any],
    *,
    to: str | list[str],
    subject: str,
    template: str,
    context: dict[str, Any],
    attachments: list[Attachment] | None = None,
) -> None:
    html = email_service.render(template, context)
    await email_service.send_email(to, subject, html, attachments, context=context)
