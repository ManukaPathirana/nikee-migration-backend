from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# Tests share one event loop (see pyproject pytest settings), so a normal pool is safe there too.
engine = create_async_engine(settings.effective_database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
