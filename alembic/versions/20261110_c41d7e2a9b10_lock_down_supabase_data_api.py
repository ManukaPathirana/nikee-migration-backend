"""lock down the Supabase Data API

Supabase publishes every table in `public` through its REST/GraphQL API to the `anon` and
`authenticated` roles (usable with the publishable key). This app talks to Postgres directly and
never uses that API, so: revoke those roles' access (now and for future tables) and enable row
level security on every table as a second guard. The app connects as the table owner, which
RLS doesn't restrict.

Does nothing on a plain PostgreSQL server (no `anon` role).

Revision ID: c41d7e2a9b10
Revises: 9b8b3ac0fe01
Create Date: 2026-11-10 17:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c41d7e2a9b10"
down_revision: str | Sequence[str] | None = "9b8b3ac0fe01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLES = "anon, authenticated"


def _on_supabase() -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")
        )
        .scalar()
    )


def _tables() -> list[str]:
    rows = op.get_bind().execute(
        sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    return [r[0] for r in rows]


def upgrade() -> None:
    if not _on_supabase():
        return
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {API_ROLES}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {API_ROLES}")
    op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {API_ROLES}")
    for kind in ("TABLES", "SEQUENCES", "FUNCTIONS"):
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON {kind} FROM {API_ROLES}"
        )
    for table in _tables():
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    if not _on_supabase():
        return
    for table in _tables():
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
