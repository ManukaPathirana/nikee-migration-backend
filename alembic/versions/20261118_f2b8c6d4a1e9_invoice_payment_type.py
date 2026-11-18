"""invoice payment type

Staff choose (or type) what each invoice is for, e.g. "Consultation Fee"; it is printed on the
invoice PDF and shown in the app and on the checkout page. Empty for older invoices.

Revision ID: f2b8c6d4a1e9
Revises: e4c1a9d2b7f3
Create Date: 2026-11-18 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b8c6d4a1e9"
down_revision: str | Sequence[str] | None = "e4c1a9d2b7f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("payment_type", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "payment_type")
