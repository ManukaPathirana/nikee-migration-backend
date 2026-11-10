"""Idempotent seed: `python -m app.scripts.seed`.

Creates the super admin (from .env), the 7 teams (visa types) with document requirements
and the company info row. In development it also adds one team admin per team
(admin.<team>@example.com), a demo staff member in Student and a demo customer.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, engine
from app.models import (
    CompanyInfo,
    CustomerProfile,
    DocumentRequirement,
    StaffProfile,
    User,
    UserRole,
    VisaType,
)
from app.services.auth_service import get_user_by_email

logger = logging.getLogger("seed")

DEMO_STAFF_EMAIL = "demo.staff@example.com"
DEMO_CUSTOMER_EMAIL = "demo.customer@example.com"

# The 7 teams. (name, slug, icon, description, [(requirement, description, mandatory)])
# Keep in step with migration 9b8b3ac0fe01, which renames/adds these on existing databases.
PASSPORT = ("Passport (bio page)", "Clear scan of the photo page, valid 6+ months.", True)
PHOTO = ("Passport-size photo", "Recent photo on a plain light background.", True)
VISA_TYPES: list[tuple[str, str, str, str, list[tuple[str, str, bool]]]] = [
    (
        "Assessment",
        "assessment",
        "fact_check",
        "Skills and qualification assessments needed before many visa applications.",
        [
            PASSPORT,
            ("Qualification certificates", "Degrees, diplomas and trade certificates.", True),
            ("Academic transcripts", "Transcripts for each qualification.", True),
            ("Employment reference letters", "Duties, dates and hours from each employer.", True),
            ("CV / résumé", "Up-to-date CV.", True),
            (PHOTO[0], PHOTO[1], False),
        ],
    ),
    (
        "Appeal",
        "appeal",
        "gavel",
        "Help with refused or cancelled visas and review applications.",
        [
            PASSPORT,
            ("Refusal or cancellation decision", "The full decision letter you received.", True),
            (
                "Original application documents",
                "What was submitted with the refused application.",
                True,
            ),
            ("Statement of your case", "Why you believe the decision should change.", True),
            ("New supporting evidence", "Anything that addresses the reasons for refusal.", False),
        ],
    ),
    (
        "Employer",
        "employer",
        "work",
        "Employer-sponsored work visas: an employer nominates you for a role.",
        [
            PASSPORT,
            (
                "Job offer / employment contract",
                "Signed offer or contract from the employer.",
                True,
            ),
            ("CV / résumé", "Up-to-date CV.", True),
            ("Qualification certificates", "Degrees, diplomas and trade certificates.", True),
            ("Work experience letters", "Reference letters from previous employers.", True),
            ("Police clearance certificate", "Issued within the last 6 months.", True),
            PHOTO,
        ],
    ),
    (
        "Family",
        "family",
        "family_restroom",
        "Join or bring a spouse, partner, child or parent.",
        [
            PASSPORT,
            (
                "Relationship evidence",
                "Marriage or birth certificate, or proof of relationship.",
                True,
            ),
            ("Sponsor's ID and status", "Sponsor's passport and visa / residency proof.", True),
            ("Financial support evidence", "Sponsor's income or bank statements.", True),
            ("Accommodation evidence", "Lease or ownership documents.", False),
            PHOTO,
        ],
    ),
    (
        "Student",
        "student",
        "school",
        "Study at a recognised university, college or school abroad.",
        [
            PASSPORT,
            ("Offer letter / CoE", "Letter of offer or Confirmation of Enrolment.", True),
            ("Academic transcripts", "Certificates and transcripts of previous study.", True),
            ("English test results", "IELTS, PTE or TOEFL score report.", True),
            (
                "Proof of funds",
                "Bank statements or sponsor letter covering fees and living costs.",
                True,
            ),
            ("Statement of purpose", "Why you chose this course and country.", False),
            PHOTO,
        ],
    ),
    (
        "Skilled",
        "skilled",
        "verified",
        "Skilled migration and permanent residency pathways.",
        [
            PASSPORT,
            ("Skills assessment", "Outcome letter from the assessing authority.", True),
            ("English test results", "IELTS, PTE or TOEFL score report.", True),
            ("Work experience evidence", "Reference letters, payslips and tax records.", True),
            ("Police clearance certificate", "For every country lived in 12+ months.", True),
            ("Medical examination", "Upload once completed.", False),
            PHOTO,
        ],
    ),
    (
        "Temporary",
        "temporary",
        "flight",
        "Visitor, working holiday and other temporary visas.",
        [
            PASSPORT,
            ("Bank statements", "Last 3–6 months of statements.", True),
            ("Travel itinerary", "Planned dates, flights and accommodation.", True),
            ("Employment / leave letter", "Letter from employer confirming approved leave.", True),
            ("Invitation letter", "From the person or company you're visiting, if any.", False),
            PHOTO,
        ],
    ),
]

COMPANY_ABOUT = (
    "Nikee Migrations is a visa and migration consultancy helping students, skilled "
    "workers, families and visitors through every step of their visa journey — from choosing "
    "the right pathway to preparing documents and lodging applications."
)


async def _ensure_user(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    role: UserRole,
    full_name: str,
    team: VisaType | None = None,
) -> tuple[User, bool]:
    user = await get_user_by_email(db, email)
    if user is not None:
        return user, False
    user = User(
        email=email.lower(),
        password_hash=hash_password(password),
        role=role,
        full_name=full_name,
        team_id=team.id if team else None,
        is_verified=True,
        is_active=True,
    )
    if role == UserRole.CUSTOMER:
        user.customer_profile = CustomerProfile(country_of_interest="Australia")
    if role in (UserRole.STAFF, UserRole.ADMIN) and team is not None:
        user.staff_profile = StaffProfile(
            designation="Team Admin" if role == UserRole.ADMIN else "Migration Consultant",
            specialization=f"{team.name} visas",
        )
    db.add(user)
    await db.flush()
    return user, True


async def seed() -> None:
    if not settings.seed_admin_password:
        raise SystemExit("SEED_ADMIN_PASSWORD is empty in .env")

    async with SessionLocal() as db:
        teams: dict[str, VisaType] = {}
        for order, (name, slug, icon, description, requirements) in enumerate(VISA_TYPES):
            visa = await db.scalar(select(VisaType).where(VisaType.slug == slug))
            if visa is None:
                visa = VisaType(
                    name=name, slug=slug, icon=icon, description=description, sort_order=order
                )
                visa.requirements = [
                    DocumentRequirement(
                        name=req, description=desc, is_mandatory=mandatory, sort_order=i
                    )
                    for i, (req, desc, mandatory) in enumerate(requirements)
                ]
                db.add(visa)
                await db.flush()
                logger.info("team / visa type %s: created", slug)
            teams[slug] = visa

        _, created = await _ensure_user(
            db,
            email=settings.seed_admin_email,
            password=settings.seed_admin_password,
            role=UserRole.SUPER_ADMIN,
            full_name=settings.seed_admin_name,
        )
        logger.info(
            "super admin %s: %s", settings.seed_admin_email, "created" if created else "exists"
        )

        if not settings.is_production:
            if not settings.seed_demo_password:
                raise SystemExit("SEED_DEMO_PASSWORD is empty in .env")
            demo: list[tuple[str, UserRole, str, VisaType | None]] = [
                (f"admin.{slug}@example.com", UserRole.ADMIN, f"{team.name} Admin", team)
                for slug, team in teams.items()
            ]
            demo += [
                (DEMO_STAFF_EMAIL, UserRole.STAFF, "Demo Staff", teams["student"]),
                (DEMO_CUSTOMER_EMAIL, UserRole.CUSTOMER, "Demo Customer", None),
            ]
            for email, role, name, team in demo:
                _, created = await _ensure_user(
                    db,
                    email=email,
                    password=settings.seed_demo_password,
                    role=role,
                    full_name=name,
                    team=team,
                )
                logger.info("%s %s: %s", role.value, email, "created" if created else "exists")

        if await db.scalar(select(CompanyInfo).limit(1)) is None:
            db.add(
                CompanyInfo(
                    name="Nikee Migrations",
                    about=COMPANY_ABOUT,
                    website_url="https://nikeemigration.com",
                    socials={},
                )
            )
            logger.info("company info: created (fill in phone/email/WhatsApp in admin settings)")

        await db.commit()
    await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(seed())
