"""google review url

The Google Business Profile "write a review" link; customers are sent there after rating us.

Revision ID: e8b1c5d3f7a2
Revises: d4f7a2c9e1b6
Create Date: 2026-11-25 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b1c5d3f7a2"
down_revision: str | Sequence[str] | None = "d4f7a2c9e1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("company_info", sa.Column("google_review_url", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("company_info", "google_review_url")
