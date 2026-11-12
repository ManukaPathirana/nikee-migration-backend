"""Background jobs.

- REDIS_URL set   -> enqueue to arq (run `arq app.workers.WorkerSettings`).
- REDIS_URL empty -> run as an in-process asyncio task (local dev without Redis).
- JOBS_EAGER=true -> run inline and await it (tests).
- On Vercel (serverless, no REDIS_URL) -> run inline before the response, errors logged.
  Vercel freezes the function once the response is sent, so a background task could be lost.

Requests never wait on email delivery except in eager mode and on Vercel.
"""

import asyncio
import logging
import os
from typing import Any

from app.core.config import settings
from app.services.email_service import Attachment

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[None]] = set()
_arq_pool: Any = None


async def _get_arq_pool() -> Any:
    global _arq_pool
    if _arq_pool is None:
        from arq import create_pool
        from arq.connections import RedisSettings

        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


async def _run_safely(name: str, kwargs: dict[str, Any]) -> None:
    from app.workers import JOB_FUNCTIONS

    try:
        await JOB_FUNCTIONS[name]({}, **kwargs)
    except Exception:
        logger.exception("Background job %s failed", name)


async def enqueue(name: str, **kwargs: Any) -> None:
    from app.workers import JOB_FUNCTIONS

    if name not in JOB_FUNCTIONS:
        raise ValueError(f"Unknown job {name!r}")

    if settings.jobs_eager:
        await JOB_FUNCTIONS[name]({}, **kwargs)
        return

    if settings.redis_url:
        pool = await _get_arq_pool()
        await pool.enqueue_job(name, **kwargs)
        return

    if os.getenv("VERCEL"):
        await _run_safely(name, kwargs)
        return

    task = asyncio.create_task(_run_safely(name, kwargs))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def send_email(
    to: str | list[str],
    subject: str,
    template: str,
    context: dict[str, Any],
    attachments: list[Attachment] | None = None,
) -> None:
    await enqueue(
        "send_email",
        to=to,
        subject=subject,
        template=template,
        context=context,
        attachments=attachments,
    )


async def shutdown(grace_seconds: float = 10) -> None:
    """Let in-process jobs finish and close the arq pool."""
    global _arq_pool
    if _background_tasks:
        await asyncio.wait(list(_background_tasks), timeout=grace_seconds)
    if _arq_pool is not None:
        await _arq_pool.aclose()
        _arq_pool = None
