"""arq worker entry point: `arq app.workers.WorkerSettings` (only used when REDIS_URL is set)."""

from arq import func
from arq.connections import RedisSettings

from app.core.config import settings
from app.workers.send_email import send_email_job

JOB_FUNCTIONS = {
    "send_email": send_email_job,
}


class WorkerSettings:
    functions = [func(fn, name=name) for name, fn in JOB_FUNCTIONS.items()]
    redis_settings = (
        RedisSettings.from_dsn(settings.redis_url) if settings.redis_url else RedisSettings()
    )
    max_tries = 5
