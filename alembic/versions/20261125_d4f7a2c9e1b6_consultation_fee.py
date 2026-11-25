"""consultation fee

Consultation inquiries carry a fee paid outside the app: the team admin sets it, the customer
uploads a receipt and the team admin approves it before the meeting is booked. Company info
gets the bank details customers pay to.

Revision ID: d4f7a2c9e1b6
Revises: c9a4e2b7d1f3
Create Date: 2026-11-25 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f7a2c9e1b6"
down_revision: str | Sequence[str] | None = "c9a4e2b7d1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = [
    sa.Column("fee_status", sa.String(20), nullable=False, server_default="none"),
    sa.Column("fee_cents", sa.Integer(), nullable=True),
    sa.Column("fee_currency", sa.String(3), nullable=False, server_default="AUD"),
    sa.Column("fee_receipt_key", sa.String(500), nullable=True),
    sa.Column("fee_receipt_name", sa.String(255), nullable=True),
    sa.Column("fee_receipt_mime", sa.String(100), nullable=True),
    sa.Column("fee_receipt_uploaded_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("fee_rejection_reason", sa.Text(), nullable=True),
    sa.Column("fee_approved_at", sa.DateTime(timezone=True), nullable=True),
]


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column("inquiries", column)
    op.add_column("company_info", sa.Column("bank_details", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("company_info", "bank_details")
    for column in reversed(COLUMNS):
        op.drop_column("inquiries", column.name)
