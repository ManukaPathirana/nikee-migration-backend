"""teams, super admin and attendance

- user_role gets 'super_admin'; existing admins become super admins.
- The 7 teams are the visa types: Assessment, Appeal, Employer, Family, Student, Skilled,
  Temporary. Existing types are renamed (employment->employer, pr->skilled,
  visitor->temporary) so their applications keep working; Assessment and Appeal are added.
- users.team_id: team admins and staff belong to one team. Existing staff join Student.
- inquiries.visa_type_id: the team handling the inquiry. Existing inquiries take their
  application's visa type, otherwise Student.
- work_sessions + activity_events for work hours and sign-in/sign-out.

Revision ID: 9b8b3ac0fe01
Revises: 7f37e3c451b6
Create Date: 2026-11-10 15:13:26.959619
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b8b3ac0fe01"
down_revision: str | Sequence[str] | None = "7f37e3c451b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# old slug -> (new slug, name, icon, description, sort order)
RENAMES = {
    "employment": ("employer", "Employer", "work",
                   "Employer-sponsored work visas: an employer nominates you for a role.", 2),
    "family": ("family", "Family", "family_restroom",
               "Join or bring a spouse, partner, child or parent.", 3),
    "student": ("student", "Student", "school",
                "Study at a recognised university, college or school abroad.", 4),
    "pr": ("skilled", "Skilled", "verified",
           "Skilled migration and permanent residency pathways.", 5),
    "visitor": ("temporary", "Temporary", "flight",
                "Visitor, working holiday and other temporary visas.", 6),
}
OLD_NAMES = {
    "employer": ("employment", "Employment Visa", 1),
    "family": ("family", "Family Visa", 2),
    "student": ("student", "Student Visa", 0),
    "skilled": ("pr", "Permanent Residency", 4),
    "temporary": ("visitor", "Visitor Visa", 3),
}
NEW_TYPES = [
    ("assessment", "Assessment", "fact_check",
     "Skills and qualification assessments needed before many visa applications.", 0, [
         ("Passport (bio page)", "Clear scan of the photo page, valid 6+ months.", True),
         ("Qualification certificates", "Degrees, diplomas and trade certificates.", True),
         ("Academic transcripts", "Transcripts for each qualification.", True),
         ("Employment reference letters", "Duties, dates and hours from each employer.", True),
         ("CV / résumé", "Up-to-date CV.", True),
         ("Passport-size photo", "Recent photo on a plain light background.", False),
     ]),
    ("appeal", "Appeal", "gavel",
     "Help with refused or cancelled visas and review applications.", 1, [
         ("Passport (bio page)", "Clear scan of the photo page, valid 6+ months.", True),
         ("Refusal or cancellation decision", "The full decision letter you received.", True),
         ("Original application documents", "What was submitted with the refused application.",
          True),
         ("Statement of your case", "Why you believe the decision should change.", True),
         ("New supporting evidence", "Anything that addresses the reasons for refusal.", False),
     ]),
]


def upgrade() -> None:
    # 1) New enum value must be committed before it can be used.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'super_admin' BEFORE 'admin'")

    # 2) Attendance tables
    op.create_table(
        "activity_events",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("device_info", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_activity_events_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_events")),
    )
    op.create_index("ix_activity_events_user_id_created_at", "activity_events",
                    ["user_id", "created_at"])
    op.create_table(
        "work_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=20), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_work_sessions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_sessions")),
    )
    op.create_index("ix_work_sessions_user_id_started_at", "work_sessions",
                    ["user_id", "started_at"])

    conn = op.get_bind()

    # 3) Visa types -> the 7 teams (only touches existing rows; the seed fills a fresh DB).
    for old, (slug, name, icon, desc, order) in RENAMES.items():
        conn.execute(
            sa.text("UPDATE visa_types SET slug=:slug, name=:name, icon=:icon, "
                    "description=:desc, sort_order=:order, updated_at=now() WHERE slug=:old"),
            {"slug": slug, "name": name, "icon": icon, "desc": desc, "order": order, "old": old},
        )
    has_types = conn.execute(sa.text("SELECT count(*) FROM visa_types")).scalar() > 0
    if has_types:
        for slug, name, icon, desc, order, reqs in NEW_TYPES:
            exists = conn.execute(sa.text("SELECT 1 FROM visa_types WHERE slug=:s"),
                                  {"s": slug}).scalar()
            if exists:
                continue
            vid = uuid.uuid4()
            conn.execute(
                sa.text("INSERT INTO visa_types (id, name, slug, description, icon, is_active, "
                        "sort_order) VALUES (:id, :name, :slug, :desc, :icon, true, :order)"),
                {"id": vid, "name": name, "slug": slug, "desc": desc, "icon": icon,
                 "order": order},
            )
            for i, (rname, rdesc, mandatory) in enumerate(reqs):
                conn.execute(
                    sa.text("INSERT INTO document_requirements (id, visa_type_id, name, "
                            "description, is_mandatory, sort_order) "
                            "VALUES (:id, :vid, :name, :desc, :m, :o)"),
                    {"id": uuid.uuid4(), "vid": vid, "name": rname, "desc": rdesc,
                     "m": mandatory, "o": i},
                )
    student_id = conn.execute(sa.text("SELECT id FROM visa_types WHERE slug='student'")).scalar()

    # 4) users.team_id; existing staff join Student; admins become super admins.
    op.add_column("users", sa.Column("team_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_users_team_id"), "users", ["team_id"])
    op.create_foreign_key(op.f("fk_users_team_id_visa_types"), "users", "visa_types",
                          ["team_id"], ["id"], ondelete="RESTRICT")
    if student_id is not None:
        conn.execute(sa.text("UPDATE users SET team_id=:t WHERE role='staff' AND team_id IS NULL"),
                     {"t": student_id})
    conn.execute(sa.text("UPDATE users SET role='super_admin' WHERE role='admin'"))
    op.create_check_constraint(
        op.f("ck_users_team_member_has_team"), "users",
        "role NOT IN ('staff', 'admin') OR team_id IS NOT NULL",
    )

    # 5) inquiries.visa_type_id (team); backfill, then require it.
    op.add_column("inquiries", sa.Column("visa_type_id", sa.Uuid(), nullable=True))
    conn.execute(sa.text(
        "UPDATE inquiries i SET visa_type_id = a.visa_type_id FROM visa_applications a "
        "WHERE i.application_id = a.id AND i.visa_type_id IS NULL"
    ))
    if student_id is not None:
        conn.execute(sa.text("UPDATE inquiries SET visa_type_id=:t WHERE visa_type_id IS NULL"),
                     {"t": student_id})
    op.alter_column("inquiries", "visa_type_id", nullable=False)
    op.create_index(op.f("ix_inquiries_visa_type_id"), "inquiries", ["visa_type_id"])
    op.create_foreign_key(op.f("fk_inquiries_visa_type_id_visa_types"), "inquiries",
                          "visa_types", ["visa_type_id"], ["id"])


def downgrade() -> None:
    conn = op.get_bind()
    op.drop_constraint(op.f("fk_inquiries_visa_type_id_visa_types"), "inquiries",
                       type_="foreignkey")
    op.drop_index(op.f("ix_inquiries_visa_type_id"), table_name="inquiries")
    op.drop_column("inquiries", "visa_type_id")

    op.drop_constraint(op.f("ck_users_team_member_has_team"), "users", type_="check")
    conn.execute(sa.text("UPDATE users SET role='admin' WHERE role IN ('super_admin', 'admin')"))
    op.drop_constraint(op.f("fk_users_team_id_visa_types"), "users", type_="foreignkey")
    op.drop_index(op.f("ix_users_team_id"), table_name="users")
    op.drop_column("users", "team_id")

    # Remove Assessment/Appeal only if nothing uses them; restore the old names.
    for slug, *_ in NEW_TYPES:
        conn.execute(sa.text(
            "DELETE FROM visa_types v WHERE v.slug=:s AND NOT EXISTS "
            "(SELECT 1 FROM visa_applications a WHERE a.visa_type_id = v.id)"
        ), {"s": slug})
    for slug, (old, name, order) in OLD_NAMES.items():
        conn.execute(sa.text("UPDATE visa_types SET slug=:old, name=:name, sort_order=:o "
                             "WHERE slug=:slug"),
                     {"old": old, "name": name, "o": order, "slug": slug})

    op.drop_index("ix_work_sessions_user_id_started_at", table_name="work_sessions")
    op.drop_table("work_sessions")
    op.drop_index("ix_activity_events_user_id_created_at", table_name="activity_events")
    op.drop_table("activity_events")
    # Note: PostgreSQL can't drop an enum value; 'super_admin' stays in user_role (unused).
