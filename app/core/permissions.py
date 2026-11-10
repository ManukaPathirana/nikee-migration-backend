"""Who can see/act on what. Teams are visa types.

- super_admin: every team, plus global settings and oversight.
- admin (team admin): only their team (users.team_id).
- staff: only their team; acts on work assigned to them.
- customer: only their own data.
"""

import uuid

from app.core.errors import Forbidden, NotFound
from app.models import User, UserRole

INTERNAL_ROLES = (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.STAFF)
ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.ADMIN)


def is_super_admin(user: User) -> bool:
    return user.role == UserRole.SUPER_ADMIN


def is_admin(user: User) -> bool:
    """Team admin or super admin."""
    return user.role in ADMIN_ROLES


def is_internal(user: User) -> bool:
    """Anyone working for the company (not a customer)."""
    return user.role in INTERNAL_ROLES


def team_scope(user: User) -> uuid.UUID | None:
    """Team to filter by: None means 'all teams' (super admin)."""
    return None if is_super_admin(user) else user.team_id


def can_access_team(user: User, team_id: uuid.UUID | None) -> bool:
    return is_super_admin(user) or (is_internal(user) and user.team_id == team_id)


def require_team(user: User, team_id: uuid.UUID | None, *, hide: bool = True) -> None:
    """Raise if an internal user works on another team's record.
    `hide=True` answers 404 so other teams' records aren't revealed."""
    if not can_access_team(user, team_id):
        if hide:
            raise NotFound("Not found")
        raise Forbidden("This belongs to another team", code="other_team")
