"""visa case flow: stages, case files, invoices, intake documents

- visa_applications: ref_no (NM-C-<year>-NNNN), stage, stage_changed_at. Existing applications
  get a number and a stage that matches their status.
- document_requirements.is_intake: passport and visa grant can be uploaded before the contract;
  everything else unlocks after the staff member approves the signed contract and deposit.
  Passport is marked intake and a "Visa grant notice" intake requirement is added to every type.
- case_files (contracts, signed copies, final application, data confirmation) and invoices.
- On Supabase the new tables get row level security like the rest (see c41d7e2a9b10).

Revision ID: d7a3e5f10b22
Revises: c41d7e2a9b10
Create Date: 2026-11-16 10:00:00
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7a3e5f10b22"
down_revision: str | Sequence[str] | None = "c41d7e2a9b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VISA_GRANT = (
    "Visa grant notice",
    "Your current or most recent visa grant letter.",
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.add_column(
        "document_requirements",
        sa.Column("is_intake", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("visa_applications", sa.Column("ref_no", sa.String(30), nullable=True))
    op.add_column(
        "visa_applications",
        sa.Column("stage", sa.String(40), server_default="intake", nullable=False),
    )
    op.add_column(
        "visa_applications",
        sa.Column("stage_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        op.f("uq_visa_applications_ref_no"), "visa_applications", ["ref_no"]
    )

    op.create_table(
        "case_files",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("file_key", sa.String(500), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("signer_name", sa.String(200), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signer_device", sa.String(255), nullable=True),
        sa.Column("recalled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recall_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["visa_applications.id"],
            name=op.f("fk_case_files_application_id_visa_applications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name=op.f("fk_case_files_uploaded_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_files")),
    )
    op.create_index(op.f("ix_case_files_application_id"), "case_files", ["application_id"])

    op.create_table(
        "invoices",
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("number", sa.String(30), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="AUD", nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("status", sa.String(20), server_default="unpaid", nullable=False),
        sa.Column("file_key", sa.String(500), nullable=True),
        sa.Column("checkout_id", sa.String(255), nullable=True),
        sa.Column("payment_ref", sa.String(255), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["visa_applications.id"],
            name=op.f("fk_invoices_application_id_visa_applications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_invoices_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
        sa.UniqueConstraint("number", name=op.f("uq_invoices_number")),
    )
    op.create_index(op.f("ix_invoices_application_id"), "invoices", ["application_id"])
    op.create_index(op.f("ix_invoices_checkout_id"), "invoices", ["checkout_id"])

    conn = op.get_bind()

    # Intake documents: passport everywhere, plus a visa grant notice right after it.
    conn.execute(
        sa.text(
            "UPDATE document_requirements SET is_intake = true WHERE name = 'Passport (bio page)'"
        )
    )
    for (vid,) in conn.execute(sa.text("SELECT id FROM visa_types")).all():
        exists = conn.execute(
            sa.text("SELECT 1 FROM document_requirements WHERE visa_type_id=:v AND name=:n"),
            {"v": vid, "n": VISA_GRANT[0]},
        ).scalar()
        if exists:
            continue
        conn.execute(
            sa.text(
                "UPDATE document_requirements SET sort_order = sort_order + 1 "
                "WHERE visa_type_id=:v AND sort_order >= 1"
            ),
            {"v": vid},
        )
        conn.execute(
            sa.text(
                "INSERT INTO document_requirements (id, visa_type_id, name, description, "
                "is_mandatory, is_intake, sort_order) VALUES (:id, :v, :n, :d, true, true, 1)"
            ),
            {"id": uuid.uuid4(), "v": vid, "n": VISA_GRANT[0], "d": VISA_GRANT[1]},
        )

    # Existing applications: a case number in creation order, and a matching stage.
    rows = conn.execute(
        sa.text(
            "SELECT id, created_at, status, assigned_staff_id FROM visa_applications "
            "ORDER BY created_at"
        )
    ).all()
    per_year: dict[int, int] = {}
    for app_id, created, status, staff in rows:
        year = created.year
        per_year[year] = per_year.get(year, 0) + 1
        stage = {
            "draft": "intake",
            "approved": "completed",
            "rejected": "withdrawn",
            "withdrawn": "withdrawn",
        }.get(status, "documents" if staff else "assignment")
        conn.execute(
            sa.text(
                "UPDATE visa_applications SET ref_no=:r, stage=:s, stage_changed_at=updated_at "
                "WHERE id=:id"
            ),
            {"r": f"NM-C-{year}-{per_year[year]:04d}", "s": stage, "id": app_id},
        )

    on_supabase = conn.execute(
        sa.text("SELECT count(*) FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")
    ).scalar()
    if on_supabase:
        for table in ("case_files", "invoices"):
            op.execute(f"REVOKE ALL ON public.{table} FROM anon, authenticated")
            op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index(op.f("ix_invoices_checkout_id"), table_name="invoices")
    op.drop_index(op.f("ix_invoices_application_id"), table_name="invoices")
    op.drop_table("invoices")
    op.drop_index(op.f("ix_case_files_application_id"), table_name="case_files")
    op.drop_table("case_files")
    op.execute("DELETE FROM document_requirements WHERE name = 'Visa grant notice' AND is_intake")
    op.drop_constraint(op.f("uq_visa_applications_ref_no"), "visa_applications", type_="unique")
    op.drop_column("visa_applications", "stage_changed_at")
    op.drop_column("visa_applications", "stage")
    op.drop_column("visa_applications", "ref_no")
    op.drop_column("document_requirements", "is_intake")
