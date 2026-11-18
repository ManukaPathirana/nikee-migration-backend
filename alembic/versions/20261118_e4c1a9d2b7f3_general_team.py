"""general team

Adds an 8th team / visa type, General, for cases that don't fit the other seven. It works like
every other team (staff, team admin, inquiries, cases, dashboard, attendance); only the data is new.
Requirements follow the same pattern: passport + visa grant notice are intake documents.

Revision ID: e4c1a9d2b7f3
Revises: d7a3e5f10b22
Create Date: 2026-11-18 12:00:00
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4c1a9d2b7f3"
down_revision: str | Sequence[str] | None = "d7a3e5f10b22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SLUG = "general"
# Keep in step with app/scripts/seed.py (VISA_TYPES).
NAME = "General"
ICON = "public"
DESCRIPTION = "General visa and migration advice, and cases that don't fit another team."
SORT_ORDER = 7
# (name, description, mandatory, intake)
REQUIREMENTS = [
    ("Passport (bio page)", "Clear scan of the photo page, valid 6+ months.", True, True),
    ("Visa grant notice", "Your current or most recent visa grant letter.", True, True),
    ("Supporting documents", "Any other documents your consultant asks for.", False, False),
    ("Passport-size photo", "Recent photo on a plain light background.", False, False),
]


def upgrade() -> None:
    conn = op.get_bind()
    # Only on databases that already have teams; the seed creates General on a fresh one.
    if not conn.execute(sa.text("SELECT count(*) FROM visa_types")).scalar():
        return
    if conn.execute(sa.text("SELECT 1 FROM visa_types WHERE slug=:s"), {"s": SLUG}).scalar():
        return
    vid = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO visa_types (id, name, slug, description, icon, is_active, sort_order) "
            "VALUES (:id, :name, :slug, :desc, :icon, true, :order)"
        ),
        {"id": vid, "name": NAME, "slug": SLUG, "desc": DESCRIPTION, "icon": ICON,
         "order": SORT_ORDER},
    )
    for i, (name, desc, mandatory, intake) in enumerate(REQUIREMENTS):
        conn.execute(
            sa.text(
                "INSERT INTO document_requirements (id, visa_type_id, name, description, "
                "is_mandatory, is_intake, sort_order) VALUES (:id, :v, :n, :d, :m, :i, :o)"
            ),
            {"id": uuid.uuid4(), "v": vid, "n": name, "d": desc, "m": mandatory, "i": intake,
             "o": i},
        )


def downgrade() -> None:
    # Remove General only if nothing uses it yet.
    op.execute(
        sa.text(
            "DELETE FROM visa_types v WHERE v.slug = :s "
            "AND NOT EXISTS (SELECT 1 FROM visa_applications a WHERE a.visa_type_id = v.id) "
            "AND NOT EXISTS (SELECT 1 FROM inquiries i WHERE i.visa_type_id = v.id) "
            "AND NOT EXISTS (SELECT 1 FROM users u WHERE u.team_id = v.id)"
        ).bindparams(s=SLUG)
    )
