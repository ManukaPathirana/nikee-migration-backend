"""Authentication: registration + OTP verification, login, rotating refresh tokens,
password reset and staff invites."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.errors import BadRequest, Conflict, Forbidden, Unauthorized
from app.core.security import (
    constant_time_equals,
    create_access_token,
    generate_otp,
    hash_otp,
    hash_password,
    hash_token,
    new_opaque_token,
    utcnow,
    verify_password,
)
from app.models import (
    CustomerProfile,
    EmailVerification,
    RefreshToken,
    User,
    UserRole,
    VerificationPurpose,
)
from app.schemas.auth import TokenPair
from app.schemas.user import UserOut
from app.services import attendance_service, job_queue

GENERIC_EMAIL_SENT = "If the account exists, we've sent an email with further instructions."


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(func.lower(User.email) == normalize_email(email)))
    return result.scalar_one_or_none()


# ---------------------------------------------------------------- one-time codes


async def _invalidate_codes(db: AsyncSession, user_id: uuid.UUID, purpose: VerificationPurpose):
    await db.execute(
        update(EmailVerification)
        .where(
            EmailVerification.user_id == user_id,
            EmailVerification.purpose == purpose,
            EmailVerification.used_at.is_(None),
        )
        .values(used_at=utcnow())
    )


async def _issue_otp(db: AsyncSession, user: User, purpose: VerificationPurpose) -> str:
    await _invalidate_codes(db, user.id, purpose)
    code = generate_otp()
    db.add(
        EmailVerification(
            user_id=user.id,
            code_hash=hash_otp(code, user.id, purpose.value),
            purpose=purpose,
            expires_at=utcnow() + timedelta(minutes=settings.otp_expire_minutes),
        )
    )
    return code


async def _consume_otp(
    db: AsyncSession, user: User, purpose: VerificationPurpose, code: str
) -> None:
    result = await db.execute(
        select(EmailVerification)
        .where(
            EmailVerification.user_id == user.id,
            EmailVerification.purpose == purpose,
            EmailVerification.used_at.is_(None),
            EmailVerification.expires_at > utcnow(),
        )
        .order_by(EmailVerification.created_at.desc())
        .limit(1)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise BadRequest("This code is invalid or has expired", code="invalid_code")
    if record.attempts >= settings.otp_max_attempts:
        raise BadRequest("Too many wrong attempts. Please request a new code.", code="code_locked")
    if not constant_time_equals(record.code_hash, hash_otp(code, user.id, purpose.value)):
        record.attempts += 1
        await db.commit()
        raise BadRequest("This code is invalid or has expired", code="invalid_code")
    record.used_at = utcnow()


# ---------------------------------------------------------------- tokens


async def issue_tokens(db: AsyncSession, user: User, device_info: str | None) -> TokenPair:
    """Create a refresh token row (caller commits) and return the token pair."""
    refresh = new_opaque_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh),
            expires_at=utcnow() + timedelta(days=settings.refresh_token_expire_days),
            device_info=(device_info or "")[:255] or None,
        )
    )
    access, expires_in = create_access_token(user.id, user.role.value)
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        user=UserOut.model_validate(user),
    )


async def revoke_all_refresh_tokens(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def _ensure_can_sign_in(user: User) -> None:
    if not user.is_active:
        raise Forbidden("This account has been deactivated", code="account_disabled")
    if not user.is_verified:
        raise Forbidden("Please verify your email before signing in", code="email_not_verified")


# ---------------------------------------------------------------- flows


async def register_customer(
    db: AsyncSession, *, email: str, password: str, full_name: str, phone: str | None
) -> None:
    email = normalize_email(email)
    password_hash = await run_in_threadpool(hash_password, password)
    user = await get_user_by_email(db, email)
    if user is not None and (user.is_verified or user.role != UserRole.CUSTOMER):
        raise Conflict("An account with this email already exists", code="email_taken")

    if user is None:
        user = User(
            email=email,
            password_hash=password_hash,
            role=UserRole.CUSTOMER,
            full_name=full_name.strip(),
            phone=phone,
        )
        user.customer_profile = CustomerProfile()
        db.add(user)
        await db.flush()
    else:
        # Unverified sign-up repeated: update details and send a fresh code.
        user.password_hash = password_hash
        user.full_name = full_name.strip()
        user.phone = phone

    code = await _issue_otp(db, user, VerificationPurpose.VERIFY)
    await db.commit()
    await _send_verification_email(user, code)


async def _send_verification_email(user: User, code: str) -> None:
    await job_queue.send_email(
        to=user.email,
        subject="Your Nikee Migrations verification code",
        template="verify_email.html",
        context={"name": user.full_name, "code": code, "minutes": settings.otp_expire_minutes},
    )


async def resend_verification(db: AsyncSession, email: str) -> None:
    user = await get_user_by_email(db, email)
    if user is None or user.is_verified or not user.is_active:
        return
    code = await _issue_otp(db, user, VerificationPurpose.VERIFY)
    await db.commit()
    await _send_verification_email(user, code)


async def verify_email(
    db: AsyncSession, *, email: str, code: str, device_info: str | None
) -> TokenPair:
    user = await get_user_by_email(db, email)
    if user is None:
        raise BadRequest("This code is invalid or has expired", code="invalid_code")
    if user.is_verified:
        raise BadRequest("This email is already verified. Please sign in.", code="already_verified")
    await _consume_otp(db, user, VerificationPurpose.VERIFY, code)
    user.is_verified = True
    if not user.is_active:
        await db.commit()
        raise Forbidden("This account has been deactivated", code="account_disabled")
    user.last_login_at = utcnow()
    tokens = await issue_tokens(db, user, device_info)
    await db.commit()
    return tokens


async def login(
    db: AsyncSession, *, email: str, password: str, device_info: str | None
) -> TokenPair:
    user = await get_user_by_email(db, email)
    ok = await run_in_threadpool(verify_password, password, user.password_hash if user else None)
    if user is None or not ok:
        raise Unauthorized("Incorrect email or password", code="invalid_credentials")
    _ensure_can_sign_in(user)
    user.last_login_at = utcnow()
    attendance_service.record_event(db, user, "login", device_info)
    tokens = await issue_tokens(db, user, device_info)
    await db.commit()
    return tokens


async def refresh(db: AsyncSession, *, refresh_token: str, device_info: str | None) -> TokenPair:
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
    )
    record = result.scalar_one_or_none()
    invalid = Unauthorized("Session expired, please sign in again", code="invalid_refresh_token")
    if record is None:
        raise invalid
    if record.revoked_at is not None:
        # Reuse of a rotated token: assume theft and end every session for this user.
        await revoke_all_refresh_tokens(db, record.user_id)
        await db.commit()
        raise invalid
    if record.expires_at <= utcnow():
        raise invalid

    user = await db.get(User, record.user_id)
    if user is None or not user.is_active or not user.is_verified:
        raise invalid

    record.revoked_at = utcnow()
    tokens = await issue_tokens(db, user, device_info or record.device_info)
    await db.commit()
    return tokens


async def logout(db: AsyncSession, refresh_token: str, device_info: str | None = None) -> None:
    record = await db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(refresh_token),
            RefreshToken.revoked_at.is_(None),
        )
    )
    if record is None:
        return
    record.revoked_at = utcnow()
    user = await db.get(User, record.user_id)
    if user is not None:
        await attendance_service.on_logout(db, user, device_info)  # ends a running shift
    await db.commit()


async def forgot_password(db: AsyncSession, email: str) -> None:
    user = await get_user_by_email(db, email)
    if user is None or not user.is_active or not user.is_verified:
        return
    code = await _issue_otp(db, user, VerificationPurpose.RESET)
    await db.commit()
    await job_queue.send_email(
        to=user.email,
        subject="Reset your Nikee Migrations password",
        template="reset_password.html",
        context={"name": user.full_name, "code": code, "minutes": settings.otp_expire_minutes},
    )


async def reset_password(db: AsyncSession, *, email: str, code: str, new_password: str) -> None:
    user = await get_user_by_email(db, email)
    if user is None or not user.is_active or not user.is_verified:
        raise BadRequest("This code is invalid or has expired", code="invalid_code")
    await _consume_otp(db, user, VerificationPurpose.RESET, code)
    user.password_hash = await run_in_threadpool(hash_password, new_password)
    await revoke_all_refresh_tokens(db, user.id)
    await db.commit()


async def change_password(
    db: AsyncSession, user: User, *, current_password: str, new_password: str
) -> None:
    ok = await run_in_threadpool(verify_password, current_password, user.password_hash)
    if not ok:
        raise BadRequest("Current password is incorrect", code="invalid_password")
    user.password_hash = await run_in_threadpool(hash_password, new_password)
    await revoke_all_refresh_tokens(db, user.id)
    await db.commit()


async def issue_invite(db: AsyncSession, user: User, invited_by: User | None) -> None:
    """Create an invite link for a staff account and email it. Commits."""
    await _invalidate_codes(db, user.id, VerificationPurpose.INVITE)
    token = new_opaque_token()
    db.add(
        EmailVerification(
            user_id=user.id,
            code_hash=hash_token(token),
            purpose=VerificationPurpose.INVITE,
            expires_at=utcnow() + timedelta(hours=settings.invite_expire_hours),
        )
    )
    await db.commit()
    await job_queue.send_email(
        to=user.email,
        subject="You're invited to Nikee Migrations",
        template="invite.html",
        context={
            "name": user.full_name,
            "invited_by": invited_by.full_name if invited_by else "The Nikee Migrations team",
            "link": f"{settings.frontend_url}/#/accept-invite?token={token}",
            "token": token,
            "hours": settings.invite_expire_hours,
        },
    )


async def accept_invite(
    db: AsyncSession, *, token: str, password: str, device_info: str | None
) -> TokenPair:
    result = await db.execute(
        select(EmailVerification).where(
            EmailVerification.code_hash == hash_token(token),
            EmailVerification.purpose == VerificationPurpose.INVITE,
            EmailVerification.used_at.is_(None),
            EmailVerification.expires_at > utcnow(),
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise BadRequest("This invite link is invalid or has expired", code="invalid_invite")
    user = await db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise BadRequest("This invite link is invalid or has expired", code="invalid_invite")

    record.used_at = utcnow()
    user.password_hash = await run_in_threadpool(hash_password, password)
    user.is_verified = True
    user.last_login_at = utcnow()
    attendance_service.record_event(db, user, "login", device_info)
    tokens = await issue_tokens(db, user, device_info)
    await db.commit()
    return tokens
