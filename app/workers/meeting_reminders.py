"""Meeting reminders (24h and 1h before). Runs every minute:
- with Redis: as an arq cron job (`arq app.workers.WorkerSettings`)
- without Redis: as a background loop started by the API (see app.main lifespan)
"""

import asyncio
import logging
from typing import Any

from app.db.session import SessionLocal
from app.services.meeting_service import send_due_reminders

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 60


async def meeting_reminders_job(ctx: dict[str, Any]) -> int:
    async with SessionLocal() as db:
        sent = await send_due_reminders(db)
    if sent:
        logger.info("Sent %d meeting reminder(s)", sent)
    return sent


async def run_reminder_loop() -> None:
    """In-process scheduler for local development without Redis."""
    while True:
        try:
            await meeting_reminders_job({})
        except Exception:
            logger.exception("Meeting reminder sweep failed")
        await asyncio.sleep(INTERVAL_SECONDS)
