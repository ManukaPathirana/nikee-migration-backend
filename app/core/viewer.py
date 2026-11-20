"""Who is looking at this response, for what customers may see.

Customers deal with their team's admin, not with the staff member handling their case: wherever a
staff member would appear in a customer's view (case, inquiry, meeting, notes, history), the team
admin is shown instead. `get_current_user` sets the viewer for each request.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.models import UserRole

_customer_viewer: ContextVar[bool] = ContextVar("customer_viewer", default=False)


def set_viewer(role: UserRole | None) -> None:
    _customer_viewer.set(role == UserRole.CUSTOMER)


def viewer_is_customer() -> bool:
    return _customer_viewer.get()


@dataclass(frozen=True)
class Contact:
    id: Any
    full_name: str
    email: str


def team_contact(team: Any) -> Contact | None:
    """The person customers see for a team: its first active team admin (by join date)."""
    admins = list(getattr(team, "team_admins", None) or [])
    if admins:
        a = admins[0]
        return Contact(a.id, a.full_name, a.email)
    if team is not None:
        return Contact(team.id, f"{team.name} team", "")
    return None


def contact_name(team: Any) -> str:
    c = team_contact(team)
    return c.full_name if c else "Your Nikee Migrations team"


def public_person(user: Any) -> Contact | None:
    """For a customer viewer: a staff member is shown as their team's admin."""
    if user is None or getattr(user, "role", None) != UserRole.STAFF:
        return None
    return team_contact(getattr(user, "team", None))
