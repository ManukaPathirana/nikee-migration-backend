"""case extra documents

Staff or the team admin can request an extra document for one case: a document requirement
with application_id set (the visa type's own list keeps application_id NULL).

Revision ID: f3c7d9a1b5e4
Revises: e8b1c5d3f7a2
Create Date: 2026-11-25 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3c7d9a1b5e4"
down_revision: str | Sequence[str] | None = "e8b1c5d3f7a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_requirements",
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("visa_applications.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_document_requirements_application_id", "document_requirements", ["application_id"]
    )


def downgrade() -> None:
    op.execute("DELETE FROM document_requirements WHERE application_id IS NOT NULL")
    op.drop_index("ix_document_requirements_application_id", "document_requirements")
    op.drop_column("document_requirements", "application_id")
