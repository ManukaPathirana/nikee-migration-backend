"""user deleted_at

The super admin can delete team admins, staff and customers. The row stays (cases, invoices and
history keep pointing at it) but login and personal details are erased and deleted_at is set.

Revision ID: b3e8d1f4c6a2
Revises: a7d3f9c2e5b1
Create Date: 2026-11-20 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3e8d1f4c6a2"
down_revision: str | Sequence[str] | None = "a7d3f9c2e5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "deleted_at")
