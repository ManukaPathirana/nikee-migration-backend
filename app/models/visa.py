import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, Text, false, text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPkMixin, pg_enum
from app.models.enums import ApplicationStatus, DocumentStatus


class VisaType(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "visa_types"

    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    # selectin: always loaded with the visa type, whichever path loaded it (async can't lazy-load).
    requirements: Mapped[list["DocumentRequirement"]] = relationship(
        back_populates="visa_type",
        cascade="all, delete-orphan",
        order_by="DocumentRequirement.sort_order",
        lazy="selectin",
    )
    # Active team admins, oldest first: the first is the name customers see (app.core.viewer).
    team_admins: Mapped[list["User"]] = relationship(  # noqa: F821
        primaryjoin=(
            "and_(VisaType.id == foreign(User.team_id), User.role == 'admin', "
            "User.is_active.is_(True), User.deleted_at.is_(None))"
        ),
        viewonly=True,
        lazy="selectin",
        order_by="User.created_at",
    )


class DocumentRequirement(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "document_requirements"

    visa_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("visa_types.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    is_mandatory: Mapped[bool] = mapped_column(default=True, server_default=true())
    # Intake documents (passport, visa grant) can be uploaded straight away; the rest unlock
    # once the staff member approves the signed contract and deposit.
    is_intake: Mapped[bool] = mapped_column(default=False, server_default=false())
    sort_order: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    visa_type: Mapped[VisaType] = relationship(back_populates="requirements")


class VisaApplication(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "visa_applications"

    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    visa_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("visa_types.id"))
    destination_country: Mapped[str] = mapped_column(String(100))
    status: Mapped[ApplicationStatus] = mapped_column(
        pg_enum(ApplicationStatus, "application_status"),
        default=ApplicationStatus.DRAFT,
        server_default=ApplicationStatus.DRAFT.value,
    )
    assigned_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    submitted_at: Mapped[datetime | None]
    # Case reference shown to everyone, e.g. NM-C-2026-0007.
    ref_no: Mapped[str | None] = mapped_column(String(30), unique=True)
    stage: Mapped[str] = mapped_column(String(40), default="intake", server_default="intake")
    stage_changed_at: Mapped[datetime | None]

    visa_type: Mapped[VisaType] = relationship()
    customer: Mapped["User"] = relationship(foreign_keys=[customer_id])  # noqa: F821
    documents: Mapped[list["ApplicationDocument"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationDocument.created_at",
    )
    assigned_staff: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[assigned_staff_id]
    )
    files: Mapped[list["CaseFile"]] = relationship(  # noqa: F821
        cascade="all, delete-orphan", order_by="CaseFile.created_at.desc()"
    )
    invoices: Mapped[list["Invoice"]] = relationship(  # noqa: F821
        cascade="all, delete-orphan", order_by="Invoice.created_at.desc()"
    )


class ApplicationDocument(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "application_documents"

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("visa_applications.id", ondelete="CASCADE"), index=True
    )
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_requirements.id", ondelete="SET NULL")
    )
    file_key: Mapped[str] = mapped_column(String(500))
    file_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[DocumentStatus] = mapped_column(
        pg_enum(DocumentStatus, "document_status"),
        default=DocumentStatus.PENDING,
        server_default=DocumentStatus.PENDING.value,
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None]

    application: Mapped[VisaApplication] = relationship(back_populates="documents")
