import os

# Must be set before any app module is imported.
os.environ["APP_ENV"] = "test"
os.environ["EMAIL_BACKEND"] = "memory"
os.environ["JOBS_EAGER"] = "true"
os.environ["REDIS_URL"] = ""
os.environ["RATE_LIMIT_ENABLED"] = "false"

from collections.abc import AsyncIterator  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, CustomerProfile, StaffProfile, User, UserRole  # noqa: E402
from app.services import email_service  # noqa: E402

PASSWORD = "Secret123"


@pytest.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_tables() -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    email_service.outbox.clear()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def create_user(
    db: AsyncSession,
    *,
    email: str,
    role: UserRole = UserRole.CUSTOMER,
    password: str | None = PASSWORD,
    verified: bool = True,
    active: bool = True,
    full_name: str = "Test User",
) -> User:
    user = User(
        email=email,
        password_hash=hash_password(password) if password else None,
        role=role,
        full_name=full_name,
        is_verified=verified,
        is_active=active,
    )
    if role == UserRole.CUSTOMER:
        user.customer_profile = CustomerProfile()
    if role == UserRole.STAFF:
        user.staff_profile = StaffProfile(designation="Consultant")
    db.add(user)
    await db.commit()
    return user


async def login(client: AsyncClient, email: str, password: str = PASSWORD) -> dict[str, Any]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def auth_header(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def last_email(to: str) -> dict[str, Any]:
    matches = [m for m in email_service.outbox if to in m["to"]]
    assert matches, f"no email sent to {to}"
    return matches[-1]
