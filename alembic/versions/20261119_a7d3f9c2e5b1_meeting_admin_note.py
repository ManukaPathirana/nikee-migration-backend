"""meeting admin note

Meetings are booked straight into one shared calendar (the super admin attends them all).
When the super admin can't make a time they decline it with a note, and staff book another.

Revision ID: a7d3f9c2e5b1
Revises: f2b8c6d4a1e9
Create Date: 2026-11-19 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3f9c2e5b1"
down_revision: str | Sequence[str] | None = "f2b8c6d4a1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("meetings", sa.Column("admin_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("meetings", "admin_note")
