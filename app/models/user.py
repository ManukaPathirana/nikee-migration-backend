import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, false, text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin, pg_enum
from app.models.enums import UserRole, VerificationPurpose


class User(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
        # Team admins and staff always belong to exactly one team (visa type).
        CheckConstraint(
            "role NOT IN ('staff', 'admin') OR team_id IS NOT NULL", name="team_member_has_team"
        ),
    )

    email: Mapped[str] = mapped_column(String(320), unique=True)
    # Null for invited staff until they accept the invite and set a password.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(pg_enum(UserRole, "user_role"))
    full_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    is_verified: Mapped[bool] = mapped_column(default=False, server_default=false())
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    last_login_at: Mapped[datetime | None]
    # The team (visa type) a team admin or staff member belongs to; null for others.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("visa_types.id", ondelete="RESTRICT"), index=True
    )

    team: Mapped["VisaType | None"] = relationship(lazy="selectin")  # noqa: F821
    customer_profile: Mapped["CustomerProfile | None"] = relationship(
        back_populates="user", lazy="selectin", cascade="all, delete-orphan"
    )
    staff_profile: Mapped["StaffProfile | None"] = relationship(
        back_populates="user",
        lazy="selectin",
        cascade="all, delete-orphan",
        foreign_keys="StaffProfile.user_id",
    )


class CustomerProfile(TimestampMixin, Base):
    __tablename__ = "customer_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    dob: Mapped[date | None]
    nationality: Mapped[str | None] = mapped_column(String(100))
    passport_no: Mapped[str | None] = mapped_column(String(50))
    address: Mapped[str | None] = mapped_column(Text)
    country_of_interest: Mapped[str | None] = mapped_column(String(100))

    user: Mapped[User] = relationship(back_populates="customer_profile")


class StaffProfile(TimestampMixin, Base):
    __tablename__ = "staff_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    designation: Mapped[str | None] = mapped_column(String(120))
    specialization: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    user: Mapped[User] = relationship(back_populates="staff_profile", foreign_keys=[user_id])


class EmailVerification(UUIDPkMixin, TimestampMixin, Base):
    """One-time codes: 6-digit OTPs (verify/reset) or invite tokens. Only hashes are stored."""

    __tablename__ = "email_verifications"
    __table_args__ = (Index("ix_email_verifications_user_purpose", "user_id", "purpose"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(128), index=True)
    purpose: Mapped[VerificationPurpose] = mapped_column(
        pg_enum(VerificationPurpose, "verification_purpose")
    )
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))


class RefreshToken(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    device_info: Mapped[str | None] = mapped_column(String(255))


class DeviceToken(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "device_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    fcm_token: Mapped[str] = mapped_column(String(512), unique=True)
    platform: Mapped[str] = mapped_column(String(20))
