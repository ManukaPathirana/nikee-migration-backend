"""Visa case extras: contract/confirmation files and invoices."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin


class CaseFile(UUIDPkMixin, TimestampMixin, Base):
    """A contract, signed copy, final application or data confirmation PDF."""

    __tablename__ = "case_files"

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("visa_applications.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40))  # CaseFileKind
    file_key: Mapped[str] = mapped_column(String(500))
    file_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Signed copies: who signed, when, from which device.
    signer_name: Mapped[str | None] = mapped_column(String(200))
    signed_at: Mapped[datetime | None]
    signer_device: Mapped[str | None] = mapped_column(String(255))
    # Staff can recall a signed copy (customer must sign again).
    recalled_at: Mapped[datetime | None]
    recall_reason: Mapped[str | None] = mapped_column(Text)


class Invoice(UUIDPkMixin, TimestampMixin, Base):
    """Deposit or final invoice, paid through the payment gateway."""

    __tablename__ = "invoices"

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("visa_applications.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))  # InvoiceKind
    number: Mapped[str] = mapped_column(String(30), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="AUD", server_default="AUD")
    description: Mapped[str] = mapped_column(String(300))
    # What the payment is for, picked or typed by staff ("Consultation Fee"); printed on the PDF.
    payment_type: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="unpaid", server_default="unpaid")
    file_key: Mapped[str | None] = mapped_column(String(500))  # generated PDF
    checkout_id: Mapped[str | None] = mapped_column(String(255), index=True)  # gateway session
    payment_ref: Mapped[str | None] = mapped_column(String(255))  # gateway payment id
    paid_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
