from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# Tests share one event loop (see pyproject pytest settings), so a normal pool is safe there too.
# No pre-ping: with a remote database (Supabase) it costs a full network round trip on every
# request. Connections are replaced after 5 minutes instead, before the pooler drops idle ones.
engine = create_async_engine(settings.effective_database_url, pool_recycle=300)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
