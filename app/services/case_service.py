"""Visa case steps after intake: assignment, contract and deposit, documents, final invoice,
data confirmation. Each step checks the stage, records who did it, and tells the other side."""

import base64
import binascii
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import PurePath

from fastapi import UploadFile
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.core.permissions import can_access_team, is_admin
from app.core.security import utcnow
from app.core.viewer import contact_name
from app.models import (
    CaseFile,
    CaseFileKind,
    CaseStage,
    CompanyInfo,
    DocumentStatus,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    User,
    UserRole,
    VisaApplication,
)
from app.services import job_queue, payment_service, pdf_service
from app.services.application_service import (
    _latest_by_requirement,
    base_query,
    get_application,
    set_stage,
    stage_of,
)
from app.services.audit_service import audit
from app.services.notification_service import notify, notify_super_admins
from app.services.storage_service import get_storage, sniff_file_type

STAGE_LABELS = {
    CaseStage.INTAKE: "Getting started",
    CaseStage.ASSIGNMENT: "Waiting for a consultant",
    CaseStage.CONTRACT_PENDING: "Consultant preparing your contract",
    CaseStage.AGREEMENT: "Sign your contract and pay the deposit",
    CaseStage.AGREEMENT_REVIEW: "Consultant checking your contract",
    CaseStage.DOCUMENTS: "Upload your documents",
    CaseStage.FINAL_PAYMENT: "Pay your final invoice",
    CaseStage.CONFIRMATION_PENDING: "Consultant preparing your final application",
    CaseStage.CONFIRMATION: "Sign your data confirmation",
    CaseStage.CONFIRMATION_REVIEW: "Consultant checking your confirmation",
    CaseStage.COMPLETED: "Completed",
    CaseStage.WITHDRAWN: "Withdrawn",
}

MAX_AMOUNT_CENTS = 100_000_00  # A$100,000


# ---------------------------------------------------------------- helpers


def parse_amount(value: str | Decimal | float | int) -> int:
    """'1800', '1,800.50' or 1800.5 -> cents."""
    try:
        amount = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation as e:
        raise BadRequest("Enter the amount in dollars, e.g. 1800", code="invalid_amount") from e
    cents = int((amount * 100).to_integral_value())
    if cents < 100 or cents > MAX_AMOUNT_CENTS:
        raise BadRequest("Amounts must be between A$1 and A$100,000", code="invalid_amount")
    return cents


def require_handler(app: VisaApplication, user: User) -> None:
    """The case's own staff member, or an admin of its team."""
    if app.assigned_staff_id == user.id:
        return
    if is_admin(user) and can_access_team(user, app.visa_type_id):
        return
    raise Forbidden("Only the case's staff member can do this", code="not_case_owner")


def require_stage(app: VisaApplication, *stages: CaseStage) -> None:
    if stage_of(app) not in stages:
        raise BadRequest(
            f"This can't be done now: the case is at '{STAGE_LABELS[stage_of(app)]}'",
            code="wrong_stage",
        )


def active_file(app: VisaApplication, kind: CaseFileKind) -> CaseFile | None:
    """Latest file of this kind that hasn't been recalled (files are newest first)."""
    for f in app.files:
        if f.kind == kind.value and f.recalled_at is None:
            return f
    return None


def invoice_of(app: VisaApplication, kind: InvoiceKind) -> Invoice | None:
    for inv in app.invoices:
        if inv.kind == kind.value and inv.status != InvoiceStatus.VOID.value:
            return inv
    return None


async def _read_pdf(upload: UploadFile, label: str) -> tuple[bytes, str]:
    data = await upload.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise BadRequest(f"The {label} must be 10 MB or smaller", code="file_too_large")
    if sniff_file_type(data) != "application/pdf":
        raise BadRequest(f"The {label} must be a PDF", code="pdf_required", status_code=415)
    name = PurePath(upload.filename or f"{label}.pdf").name[:200] or f"{label}.pdf"
    return data, name


async def _store_file(
    db: AsyncSession,
    app: VisaApplication,
    kind: CaseFileKind,
    data: bytes,
    name: str,
    user: User,
    **extra,
) -> CaseFile:
    key = f"cases/{app.id}/{kind.value}-{uuid.uuid4().hex}.pdf"
    await get_storage().save(key, data, "application/pdf")
    f = CaseFile(
        application_id=app.id,
        kind=kind.value,
        file_key=key,
        file_name=name,
        mime_type="application/pdf",
        size_bytes=len(data),
        uploaded_by=user.id,
        **extra,
    )
    db.add(f)
    return f


async def _next_invoice_number(db: AsyncSession) -> str:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('invoice_number'))"))
    prefix = f"NM-INV-{utcnow().year}-"
    last = await db.scalar(
        select(Invoice.number)
        .where(Invoice.number.like(f"{prefix}%"))
        .order_by(func.length(Invoice.number).desc(), Invoice.number.desc())
        .limit(1)
    )
    n = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{n:04d}"


async def _company(db: AsyncSession) -> tuple[str, list[str]]:
    c = await db.scalar(select(CompanyInfo).limit(1))
    if c is None:
        return "Nikee Migrations", []
    lines = [x for x in (c.address, c.phone, c.email, c.website_url) if x]
    return c.name, lines


async def _render_invoice(db: AsyncSession, app: VisaApplication, inv: Invoice) -> None:
    name, lines = await _company(db)
    pdf = pdf_service.invoice_pdf(
        company_name=name,
        company_lines=lines,
        number=inv.number,
        issued=inv.created_at or utcnow(),
        customer_name=app.customer.full_name,
        customer_email=app.customer.email,
        case_ref=app.ref_no or "",
        visa_type=app.visa_type.name,
        description=inv.description,
        payment_type=inv.payment_type or invoice_label(inv.kind),
        amount_cents=inv.amount_cents,
        currency=inv.currency,
        paid_at=inv.paid_at,
        payment_ref=inv.payment_ref,
    )
    key = inv.file_key or f"cases/{app.id}/invoice-{inv.number}.pdf"
    await get_storage().save(key, pdf, "application/pdf")
    inv.file_key = key


def invoice_label(kind: str) -> str:
    return "Deposit (50%)" if kind == InvoiceKind.DEPOSIT.value else "Final payment"


def clean_payment_type(value: str | None) -> str | None:
    text = " ".join((value or "").split())  # one line, single spaces
    if text and len(text) < 3:
        raise BadRequest("Payment type is too short", code="invalid_payment_type")
    return text or None


def _describe(app: VisaApplication, label: str) -> str:
    return f"{label} · {app.visa_type.name} visa services · Case {app.ref_no}"


async def _create_invoice(
    db: AsyncSession,
    app: VisaApplication,
    kind: InvoiceKind,
    cents: int,
    user: User,
    payment_type: str | None = None,
) -> Invoice:
    label = payment_type or invoice_label(kind.value)
    inv = Invoice(
        application_id=app.id,
        kind=kind.value,
        number=await _next_invoice_number(db),
        amount_cents=cents,
        currency=settings.currency,
        payment_type=payment_type,
        description=_describe(app, label),
        status=InvoiceStatus.UNPAID.value,
        created_by=user.id,
        created_at=utcnow(),
    )
    db.add(inv)
    await db.flush()
    await _render_invoice(db, app, inv)
    return inv


def _tell(
    db: AsyncSession, app: VisaApplication, user_id: uuid.UUID | None, title: str, body: str
) -> None:
    if user_id is None:
        return
    notify(
        db,
        user_id=user_id,
        type="case_update",
        title=title,
        body=body,
        data={"application_id": str(app.id), "stage": app.stage},
    )


async def _email_customer(app: VisaApplication, heading: str, message: str) -> None:
    await job_queue.send_email(
        to=app.customer.email,
        subject=f"{heading} · {app.ref_no}",
        template="inquiry_update.html",
        context={
            "name": app.customer.full_name,
            "heading": heading,
            "message": message,
            "ref_no": app.ref_no,
            "subject": f"{app.visa_type.name} visa",
            "status": STAGE_LABELS[stage_of(app)],
        },
    )


async def _reload(db: AsyncSession, app: VisaApplication, user: User) -> VisaApplication:
    return await get_application(db, app.id, user)


# ---------------------------------------------------------------- team admin


async def search_customers(db: AsyncSession, q: str, limit: int = 10) -> list[User]:
    """Team admins pick the customer to start a case for (name, email or phone)."""
    like = f"%{q.strip()}%"
    return list(
        await db.scalars(
            select(User)
            .where(
                User.role == UserRole.CUSTOMER,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                or_(User.full_name.ilike(like), User.email.ilike(like), User.phone.ilike(like)),
            )
            .order_by(User.full_name)
            .limit(limit)
        )
    )


async def start_case(
    db: AsyncSession,
    admin: User,
    *,
    customer_id: uuid.UUID | None,
    email: str | None,
    full_name: str | None,
    phone: str | None,
    visa_type_id: uuid.UUID | None,
    destination_country: str,
    note: str | None,
) -> VisaApplication:
    """Team admin opens a visa case for a customer: it waits for their first step (passport and
    visa grant). A new customer gets an account to claim by signing up with that email."""
    from app.models import CustomerProfile, VisaType
    from app.services.application_service import next_case_ref
    from app.services.auth_service import get_user_by_email, normalize_email

    team_id = admin.team_id if admin.role == UserRole.ADMIN else visa_type_id
    if team_id is None:
        raise BadRequest("Choose the visa type (team)", code="team_required")
    if admin.role == UserRole.ADMIN and visa_type_id not in (None, admin.team_id):
        raise Forbidden("You can start cases for your own team only")
    visa = await db.get(VisaType, team_id)
    if visa is None or not visa.is_active:
        raise BadRequest("This visa type isn't available", code="invalid_visa_type")

    new_account = False
    if customer_id is not None:
        customer = await db.get(User, customer_id)
        if (
            customer is None
            or customer.role != UserRole.CUSTOMER
            or customer.deleted_at is not None
            or not customer.is_active
        ):
            raise NotFound("Customer not found")
    else:
        customer = await get_user_by_email(db, email or "")
        if customer is not None and (
            customer.role != UserRole.CUSTOMER or customer.deleted_at is not None
        ):
            raise BadRequest(
                "This email belongs to a staff or admin account", code="not_a_customer"
            )
        if customer is not None and not customer.is_active:
            raise BadRequest("This customer's account is deactivated", code="inactive_customer")
        if customer is None:
            customer = User(
                email=normalize_email(email or ""),
                password_hash=None,  # set when they sign up with this email
                role=UserRole.CUSTOMER,
                full_name=(full_name or "").strip(),
                phone=(phone or "").strip() or None,
                is_verified=False,
            )
            customer.customer_profile = CustomerProfile()
            db.add(customer)
            await db.flush()
            new_account = True

    open_case = await db.scalar(
        select(VisaApplication.ref_no).where(
            VisaApplication.customer_id == customer.id,
            VisaApplication.visa_type_id == visa.id,
            VisaApplication.stage.notin_([CaseStage.COMPLETED.value, CaseStage.WITHDRAWN.value]),
        )
    )
    if open_case:
        raise Conflict(
            f"{customer.full_name} already has an open {visa.name} case ({open_case})",
            code="case_exists",
        )

    app = VisaApplication(
        customer_id=customer.id,
        visa_type_id=visa.id,
        destination_country=destination_country.strip(),
        ref_no=await next_case_ref(db),
        stage=CaseStage.INTAKE.value,
        stage_changed_at=utcnow(),
    )
    db.add(app)
    await db.flush()
    audit(
        db,
        actor_id=admin.id,
        action="case.start",
        entity="visa_application",
        entity_id=app.id,
        meta={"customer_id": str(customer.id), "new_account": new_account},
    )
    # Customers see the team admin: the one starting it, or the team's first admin.
    who = admin.full_name if admin.role == UserRole.ADMIN else None
    if who is None:
        who = await db.scalar(
            select(User.full_name)
            .where(
                User.role == UserRole.ADMIN,
                User.team_id == visa.id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
            .order_by(User.created_at)
            .limit(1)
        )
    who = who or f"The {visa.name} team"
    note = (note or "").strip() or None
    notify(
        db,
        user_id=customer.id,
        type="case_update",
        title=f"Your {visa.name} visa case has started",
        body=f"{who} opened case {app.ref_no}. Upload your passport and visa grant to begin.",
        data={"application_id": str(app.id), "stage": app.stage},
    )
    await db.commit()

    web = settings.web_app_url.rstrip("/")
    if new_account:
        heading = "Your visa case is ready"
        message = (
            f"{who} from Nikee Migrations has opened your {visa.name} visa case. Sign up with "
            f"this email address ({customer.email}) in the Nikee Migrations app or website, "
            "then upload your passport and visa grant to begin."
        )
        link, link_label = f"{web}/#/register", "Sign up"
    else:
        heading = "Your visa case has started"
        message = (
            f"{who} has opened your {visa.name} visa case. Open the Nikee Migrations app and "
            "upload your passport and visa grant to begin."
        )
        link, link_label = f"{web}/#/customer/applications/{app.id}", "Open my case"
    await job_queue.send_email(
        to=customer.email,
        subject=f"{heading} · {app.ref_no}",
        template="inquiry_update.html",
        context={
            "name": customer.full_name,
            "heading": heading,
            "message": message,
            "reason": note,
            "ref_no": app.ref_no,
            "subject": f"{visa.name} visa",
            "status": STAGE_LABELS[CaseStage.INTAKE],
            "link": link,
            "link_label": link_label,
        },
    )
    return await get_application(db, app.id, admin)


async def assign(
    db: AsyncSession, app: VisaApplication, actor: User, staff_id: uuid.UUID
) -> VisaApplication:
    if not (is_admin(actor) and can_access_team(actor, app.visa_type_id)):
        raise Forbidden("Only this team's admin can assign cases")
    if stage_of(app) in (CaseStage.INTAKE, CaseStage.COMPLETED, CaseStage.WITHDRAWN):
        raise BadRequest("This case can't be assigned now", code="wrong_stage")
    staff = await db.get(User, staff_id)
    if staff is None or staff.role != UserRole.STAFF or not staff.is_active:
        raise BadRequest("Choose an active staff member", code="invalid_staff")
    if staff.team_id != app.visa_type_id:
        raise BadRequest("Choose a staff member from this case's team", code="wrong_team")
    previous = app.assigned_staff_id
    app.assigned_staff_id = staff.id
    if stage_of(app) == CaseStage.ASSIGNMENT:
        set_stage(app, CaseStage.CONTRACT_PENDING)
    audit(
        db,
        actor_id=actor.id,
        action="case.assign",
        entity="visa_application",
        entity_id=app.id,
        meta={"staff_id": str(staff.id), "previous": str(previous) if previous else None},
    )
    _tell(
        db,
        app,
        staff.id,
        f"New case {app.ref_no}",
        f"{app.customer.full_name} · {app.visa_type.name}. Send the contract and deposit invoice.",
    )
    _tell(
        db,
        app,
        app.customer_id,
        "Your consultant is ready",
        f"{contact_name(app.visa_type)} will look after your {app.visa_type.name} visa case.",
    )
    await db.commit()
    return await _reload(db, app, actor)


# ---------------------------------------------------------------- contract and deposit


async def send_agreement(
    db: AsyncSession,
    app: VisaApplication,
    staff: User,
    *,
    contract: UploadFile,
    deposit: str,
    payment_type: str | None = None,
) -> VisaApplication:
    require_handler(app, staff)
    require_stage(app, CaseStage.CONTRACT_PENDING)
    cents = parse_amount(deposit)
    kind_label = clean_payment_type(payment_type)
    data, name = await _read_pdf(contract, "contract")
    await _store_file(db, app, CaseFileKind.CONTRACT, data, name, staff)
    inv = await _create_invoice(db, app, InvoiceKind.DEPOSIT, cents, staff, kind_label)
    set_stage(app, CaseStage.AGREEMENT)
    audit(
        db,
        actor_id=staff.id,
        action="case.send_agreement",
        entity="visa_application",
        entity_id=app.id,
        meta={"invoice": inv.number, "amount_cents": cents, "payment_type": inv.payment_type},
    )
    _tell(
        db,
        app,
        app.customer_id,
        "Your contract and deposit invoice are ready",
        f"Sign the contract and pay the deposit of {pdf_service.money(cents)} in the app.",
    )
    await db.commit()
    app = await _reload(db, app, staff)
    await _email_customer(
        app,
        "Your contract is ready",
        "Open the Nikee Migrations app to sign your contract and pay the deposit.",
    )
    return app


async def _maybe_finish_agreement(db: AsyncSession, app: VisaApplication) -> None:
    """Agreement step is complete once the contract is signed and the deposit paid."""
    deposit = invoice_of(app, InvoiceKind.DEPOSIT)
    signed = active_file(app, CaseFileKind.SIGNED_CONTRACT)
    if (
        stage_of(app) == CaseStage.AGREEMENT
        and signed is not None
        and deposit is not None
        and deposit.status == InvoiceStatus.PAID.value
    ):
        set_stage(app, CaseStage.AGREEMENT_REVIEW)
        _tell(
            db,
            app,
            app.assigned_staff_id,
            f"{app.ref_no}: contract signed and deposit paid",
            f"{app.customer.full_name} is ready for your review.",
        )


async def approve_agreement(db: AsyncSession, app: VisaApplication, staff: User) -> VisaApplication:
    require_handler(app, staff)
    require_stage(app, CaseStage.AGREEMENT_REVIEW)
    set_stage(app, CaseStage.DOCUMENTS)
    audit(
        db,
        actor_id=staff.id,
        action="case.approve_agreement",
        entity="visa_application",
        entity_id=app.id,
    )
    _tell(
        db,
        app,
        app.customer_id,
        "Your documents are unlocked",
        "Thanks for signing and paying. Upload the rest of your documents now.",
    )
    await db.commit()
    app = await _reload(db, app, staff)
    await _email_customer(
        app,
        "Upload your documents",
        "Your contract and deposit are approved. "
        "Your full document checklist is now open in the app.",
    )
    return app


async def recall_agreement(
    db: AsyncSession, app: VisaApplication, staff: User, reason: str
) -> VisaApplication:
    """The signed contract isn't right: the customer signs again."""
    require_handler(app, staff)
    require_stage(app, CaseStage.AGREEMENT_REVIEW)
    signed = active_file(app, CaseFileKind.SIGNED_CONTRACT)
    if signed is not None:
        signed.recalled_at = utcnow()
        signed.recall_reason = reason.strip()
    set_stage(app, CaseStage.AGREEMENT)
    audit(
        db,
        actor_id=staff.id,
        action="case.recall_contract",
        entity="visa_application",
        entity_id=app.id,
        meta={"reason": reason.strip()},
    )
    _tell(db, app, app.customer_id, "Please sign your contract again", reason.strip())
    await db.commit()
    app = await _reload(db, app, staff)
    await _email_customer(app, "Please sign your contract again", reason.strip())
    return app


# ---------------------------------------------------------------- signing


async def sign(
    db: AsyncSession,
    app: VisaApplication,
    customer: User,
    *,
    kind: CaseFileKind,
    signer_name: str,
    signature_png_b64: str,
    device: str | None,
) -> VisaApplication:
    if app.customer_id != customer.id:
        raise Forbidden("Only the applicant can sign")
    if kind == CaseFileKind.CONTRACT:
        require_stage(app, CaseStage.AGREEMENT)
        source = active_file(app, CaseFileKind.CONTRACT)
        target, title = CaseFileKind.SIGNED_CONTRACT, "Contract"
    elif kind == CaseFileKind.DATA_CONFIRMATION:
        require_stage(app, CaseStage.CONFIRMATION)
        source = active_file(app, CaseFileKind.DATA_CONFIRMATION)
        target, title = CaseFileKind.SIGNED_DATA_CONFIRMATION, "Data confirmation"
    else:
        raise BadRequest("Only the contract and data confirmation are signed", code="invalid_kind")
    if source is None:
        raise BadRequest("There's nothing to sign yet", code="nothing_to_sign")
    if active_file(app, target) is not None:
        raise BadRequest("You've already signed this", code="already_signed")
    try:
        png = base64.b64decode(signature_png_b64.split(",")[-1], validate=True)
    except (binascii.Error, ValueError) as e:
        raise BadRequest("The signature couldn't be read", code="invalid_signature") from e
    if not png.startswith(b"\x89PNG") or len(png) < 100 or len(png) > 2_000_000:
        raise BadRequest("The signature couldn't be read", code="invalid_signature")

    now = utcnow()
    original = await get_storage().read(source.file_key)
    signed_pdf = pdf_service.sign_pdf(
        original,
        signature_png=png,
        signer_name=signer_name.strip(),
        signed_at=now,
        document_title=title,
        case_ref=app.ref_no or "",
        device=device,
    )
    stem = PurePath(source.file_name).stem
    await _store_file(
        db,
        app,
        target,
        signed_pdf,
        f"{stem}-signed.pdf",
        customer,
        signer_name=signer_name.strip(),
        signed_at=now,
        signer_device=(device or "")[:255] or None,
    )
    await db.flush()
    app = await _reload(db, app, customer)
    audit(
        db,
        actor_id=customer.id,
        action=f"case.sign_{kind.value}",
        entity="visa_application",
        entity_id=app.id,
    )
    if kind == CaseFileKind.CONTRACT:
        await _maybe_finish_agreement(db, app)
    else:
        set_stage(app, CaseStage.CONFIRMATION_REVIEW)
        _tell(
            db,
            app,
            app.assigned_staff_id,
            f"{app.ref_no}: data confirmation signed",
            f"{app.customer.full_name} signed the data confirmation. "
            "Check it and complete the case.",
        )
    await db.commit()
    return await _reload(db, app, customer)


# ---------------------------------------------------------------- payments


async def get_invoice(
    db: AsyncSession, invoice_id: uuid.UUID, user: User
) -> tuple[Invoice, VisaApplication]:
    inv = await db.get(Invoice, invoice_id)
    if inv is None:
        raise NotFound("Invoice not found")
    app = await get_application(db, inv.application_id, user)  # enforces access
    return inv, app


async def checkout(
    db: AsyncSession,
    inv: Invoice,
    app: VisaApplication,
    customer: User,
    payment_type: str | None = None,
) -> str:
    """Starts a checkout. The customer may first say what the payment is for (saved on the invoice
    and printed on its PDF)."""
    if app.customer_id != customer.id:
        raise Forbidden("Only the applicant can pay this invoice")
    if inv.status != InvoiceStatus.UNPAID.value:
        raise BadRequest("This invoice is already paid", code="already_paid")
    chosen = clean_payment_type(payment_type)
    if chosen and chosen != inv.payment_type:
        inv.payment_type = chosen
        inv.description = _describe(app, chosen)
        await _render_invoice(db, app, inv)
        audit(
            db,
            actor_id=customer.id,
            action="invoice.payment_type",
            entity="invoice",
            entity_id=inv.id,
            meta={"number": inv.number, "payment_type": chosen},
        )
    result = await payment_service.create_checkout(
        invoice_id=inv.id,
        number=inv.number,
        title=inv.payment_type or invoice_label(inv.kind),
        description=inv.description,
        amount_cents=inv.amount_cents,
        currency=inv.currency,
        customer_email=app.customer.email,
    )
    inv.checkout_id = result.checkout_id
    await db.commit()
    return result.url


async def mark_paid(db: AsyncSession, inv: Invoice, payment_ref: str) -> None:
    """Records a payment (gateway confirmed) and moves the case on. Idempotent."""
    if inv.status == InvoiceStatus.PAID.value:
        return
    app = await db.scalar(base_query().where(VisaApplication.id == inv.application_id))
    inv.status = InvoiceStatus.PAID.value
    inv.paid_at = utcnow()
    inv.payment_ref = payment_ref
    await _render_invoice(db, app, inv)
    audit(
        db,
        actor_id=app.customer_id,
        action="invoice.paid",
        entity="invoice",
        entity_id=inv.id,
        meta={"number": inv.number, "amount_cents": inv.amount_cents, "ref": payment_ref},
    )
    _tell(
        db,
        app,
        app.customer_id,
        f"Payment received · {inv.number}",
        f"Thanks, we received {pdf_service.money(inv.amount_cents, inv.currency)}.",
    )
    _tell(
        db,
        app,
        app.assigned_staff_id,
        f"{app.ref_no}: {inv.kind} invoice paid",
        f"{app.customer.full_name} paid {pdf_service.money(inv.amount_cents, inv.currency)}.",
    )
    await notify_super_admins(
        db,
        type="payment_received",
        title=f"Payment received: {app.ref_no}",
        body=f"{app.customer.full_name} paid {pdf_service.money(inv.amount_cents, inv.currency)}"
        f" ({inv.payment_type or invoice_label(inv.kind)}, {inv.number}).",
        data={"application_id": str(app.id)},
    )
    await db.flush()
    if inv.kind == InvoiceKind.DEPOSIT.value:
        await _maybe_finish_agreement(db, app)
    elif stage_of(app) == CaseStage.FINAL_PAYMENT:
        set_stage(app, CaseStage.CONFIRMATION_PENDING)
    await db.commit()


async def confirm_payment(db: AsyncSession, inv: Invoice) -> bool:
    """Asks the gateway whether the checkout was paid (for when the webhook is slow or absent)."""
    if inv.status == InvoiceStatus.PAID.value:
        return True
    if not inv.checkout_id:
        return False
    ref = await payment_service.check_paid(inv.checkout_id)
    if ref is None:
        return False
    await mark_paid(db, inv, ref)
    return True


# ---------------------------------------------------------------- documents -> final invoice


async def finalize(
    db: AsyncSession,
    app: VisaApplication,
    staff: User,
    final_amount: str,
    payment_type: str | None = None,
) -> VisaApplication:
    require_handler(app, staff)
    require_stage(app, CaseStage.DOCUMENTS)
    latest = _latest_by_requirement(app)
    missing = [
        r.name
        for r in app.visa_type.requirements
        if r.is_mandatory and (r.id not in latest or latest[r.id].status != DocumentStatus.APPROVED)
    ]
    if missing:
        raise BadRequest(
            f"Approve every required document first "
            f"({len(missing)} left: {', '.join(missing[:3])})",
            code="documents_not_approved",
        )
    cents = parse_amount(final_amount)
    kind_label = clean_payment_type(payment_type)
    inv = await _create_invoice(db, app, InvoiceKind.FINAL, cents, staff, kind_label)
    set_stage(app, CaseStage.FINAL_PAYMENT)
    audit(
        db,
        actor_id=staff.id,
        action="case.finalize",
        entity="visa_application",
        entity_id=app.id,
        meta={"invoice": inv.number, "amount_cents": cents, "payment_type": inv.payment_type},
    )
    _tell(
        db,
        app,
        app.customer_id,
        "Your application is finalized",
        f"Pay the final invoice of {pdf_service.money(cents)} in the app.",
    )
    await db.commit()
    app = await _reload(db, app, staff)
    await _email_customer(
        app, "Your final invoice is ready", "Open the app to pay the final invoice securely."
    )
    return app


# ---------------------------------------------------------------- confirmation


async def send_confirmation(
    db: AsyncSession,
    app: VisaApplication,
    staff: User,
    *,
    final_application: UploadFile,
    data_confirmation: UploadFile,
) -> VisaApplication:
    require_handler(app, staff)
    require_stage(app, CaseStage.CONFIRMATION_PENDING)
    a_data, a_name = await _read_pdf(final_application, "final application")
    c_data, c_name = await _read_pdf(data_confirmation, "data confirmation")
    await _store_file(db, app, CaseFileKind.FINAL_APPLICATION, a_data, a_name, staff)
    await _store_file(db, app, CaseFileKind.DATA_CONFIRMATION, c_data, c_name, staff)
    set_stage(app, CaseStage.CONFIRMATION)
    audit(
        db,
        actor_id=staff.id,
        action="case.send_confirmation",
        entity="visa_application",
        entity_id=app.id,
    )
    _tell(
        db,
        app,
        app.customer_id,
        "Check and sign your data confirmation",
        "Your finalized application is ready. Review it and sign the data confirmation.",
    )
    await db.commit()
    app = await _reload(db, app, staff)
    await _email_customer(
        app,
        "Sign your data confirmation",
        "Your finalized application is ready in the app. "
        "Please review it and sign the data confirmation.",
    )
    return app


async def request_confirmation_changes(
    db: AsyncSession, app: VisaApplication, customer: User, reason: str
) -> VisaApplication:
    """Customer: something in the finalized application or the data confirmation is wrong.
    Both go back to the staff member, who fixes them and sends new ones."""
    if app.customer_id != customer.id:
        raise Forbidden("Only the applicant can do this")
    require_stage(app, CaseStage.CONFIRMATION)
    reason = reason.strip()
    now = utcnow()
    for kind in (CaseFileKind.FINAL_APPLICATION, CaseFileKind.DATA_CONFIRMATION):
        f = active_file(app, kind)
        if f is not None:
            f.recalled_at = now
            f.recall_reason = reason
    set_stage(app, CaseStage.CONFIRMATION_PENDING)
    audit(
        db,
        actor_id=customer.id,
        action="case.request_changes",
        entity="visa_application",
        entity_id=app.id,
        meta={"reason": reason},
    )
    _tell(
        db,
        app,
        app.assigned_staff_id,
        f"{app.ref_no}: customer asked for changes",
        f"{app.customer.full_name}: {reason}. Fix the papers and send them again.",
    )
    await db.commit()
    return await _reload(db, app, customer)


async def recall_confirmation(
    db: AsyncSession, app: VisaApplication, staff: User, reason: str
) -> VisaApplication:
    require_handler(app, staff)
    require_stage(app, CaseStage.CONFIRMATION_REVIEW)
    signed = active_file(app, CaseFileKind.SIGNED_DATA_CONFIRMATION)
    if signed is not None:
        signed.recalled_at = utcnow()
        signed.recall_reason = reason.strip()
    set_stage(app, CaseStage.CONFIRMATION)
    audit(
        db,
        actor_id=staff.id,
        action="case.recall_confirmation",
        entity="visa_application",
        entity_id=app.id,
        meta={"reason": reason.strip()},
    )
    _tell(db, app, app.customer_id, "Please sign your data confirmation again", reason.strip())
    await db.commit()
    return await _reload(db, app, staff)


async def complete(db: AsyncSession, app: VisaApplication, staff: User) -> VisaApplication:
    require_handler(app, staff)
    require_stage(app, CaseStage.CONFIRMATION_REVIEW)
    set_stage(app, CaseStage.COMPLETED)
    audit(
        db, actor_id=staff.id, action="case.complete", entity="visa_application", entity_id=app.id
    )
    _tell(
        db,
        app,
        app.customer_id,
        "Your case is complete",
        f"Thank you. Your {app.visa_type.name} visa case {app.ref_no} is complete.",
    )
    await db.commit()
    app = await _reload(db, app, staff)
    await _email_customer(app, "Your case is complete", "Thank you for choosing Nikee Migrations.")
    return app


async def file_url(db: AsyncSession, file_id: uuid.UUID, user: User) -> tuple[str, CaseFile]:
    f = await db.get(CaseFile, file_id)
    if f is None:
        raise NotFound("File not found")
    await get_application(db, f.application_id, user)  # enforces access
    return await get_storage().download_url(f.file_key, f.file_name, f.mime_type), f


async def invoice_url(db: AsyncSession, invoice_id: uuid.UUID, user: User) -> str:
    inv, app = await get_invoice(db, invoice_id, user)
    if not inv.file_key:
        await _render_invoice(db, app, inv)
        await db.commit()
    return await get_storage().download_url(inv.file_key, f"{inv.number}.pdf", "application/pdf")
