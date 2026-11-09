from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

_engine_kwargs: dict[str, Any] = {"pool_pre_ping": True}
if settings.is_test:
    _engine_kwargs = {"poolclass": NullPool}

engine = create_async_engine(settings.effective_database_url, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
