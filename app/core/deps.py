import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, Unauthorized
from app.core.security import decode_access_token
from app.db.session import SessionLocal
from app.models import User, UserRole

_bearer = HTTPBearer(auto_error=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None:
        raise Unauthorized("Missing bearer token", code="not_authenticated")
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise Unauthorized("Invalid or expired access token", code="invalid_token")
    try:
        user_id = uuid.UUID(payload["sub"])
    except ValueError as exc:
        raise Unauthorized("Invalid access token", code="invalid_token") from exc
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthorized("Account not found or disabled", code="invalid_token")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: UserRole) -> Callable[..., Coroutine[Any, Any, User]]:
    """Dependency factory: `Depends(require_role(UserRole.ADMIN, UserRole.STAFF))`."""

    async def _dependency(user: CurrentUser) -> User:
        if user.role not in roles:
            raise Forbidden("You do not have permission to do this")
        return user

    return _dependency


AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]
StaffOrAdmin = Annotated[User, Depends(require_role(UserRole.STAFF, UserRole.ADMIN))]
CustomerUser = Annotated[User, Depends(require_role(UserRole.CUSTOMER))]
