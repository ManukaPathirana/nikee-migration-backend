"""inquiry kind

Two kinds of inquiry: "consultation" (meet the migration agent, booked in the agent's shared
calendar by the assigned staff member) and "general" (handled by the team admin, meetings in the
team's own calendar). Existing inquiries are consultations.

Revision ID: c9a4e2b7d1f3
Revises: b3e8d1f4c6a2
Create Date: 2026-11-20 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9a4e2b7d1f3"
down_revision: str | Sequence[str] | None = "b3e8d1f4c6a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inquiries",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="consultation"),
    )


def downgrade() -> None:
    op.drop_column("inquiries", "kind")
