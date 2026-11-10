from fastapi import APIRouter, Request, Response, status

from app.core.deps import CurrentUser, DbSession
from app.core.rate_limit import limiter
from app.schemas.auth import (
    AcceptInviteIn,
    ChangePasswordIn,
    EmailIn,
    LoginIn,
    RefreshIn,
    RegisterIn,
    ResetPasswordIn,
    TokenPair,
    VerifyEmailIn,
)
from app.schemas.common import MessageOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _device(request: Request) -> str | None:
    return request.headers.get("user-agent")


@router.post("/register", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(request: Request, data: RegisterIn, db: DbSession) -> MessageOut:
    """Customer self sign-up. Sends a 6-digit code to verify the email."""
    await auth_service.register_customer(
        db, email=data.email, password=data.password, full_name=data.full_name, phone=data.phone
    )
    return MessageOut(message="We've sent a 6-digit verification code to your email.")


@router.post("/verify-email", response_model=TokenPair)
@limiter.limit("10/minute")
async def verify_email(request: Request, data: VerifyEmailIn, db: DbSession) -> TokenPair:
    """Verify the email with the OTP. Signs the user in on success."""
    return await auth_service.verify_email(
        db, email=data.email, code=data.code, device_info=_device(request)
    )


@router.post("/resend-verification", response_model=MessageOut)
@limiter.limit("3/minute")
async def resend_verification(request: Request, data: EmailIn, db: DbSession) -> MessageOut:
    await auth_service.resend_verification(db, data.email)
    return MessageOut(message="If your account still needs verifying, we've sent a new code.")


@router.post("/login", response_model=TokenPair)
@limiter.limit("10/minute")
async def login(request: Request, data: LoginIn, db: DbSession) -> TokenPair:
    return await auth_service.login(
        db, email=data.email, password=data.password, device_info=_device(request)
    )


@router.post("/refresh", response_model=TokenPair)
@limiter.limit("30/minute")
async def refresh(request: Request, data: RefreshIn, db: DbSession) -> TokenPair:
    """Exchange a refresh token for a new pair. The old refresh token stops working."""
    return await auth_service.refresh(
        db, refresh_token=data.refresh_token, device_info=_device(request)
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, data: RefreshIn, db: DbSession) -> Response:
    """Revokes the refresh token. For staff/admins this also logs the sign-out and ends a
    running work shift."""
    await auth_service.logout(db, data.refresh_token, _device(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/forgot-password", response_model=MessageOut)
@limiter.limit("3/minute")
async def forgot_password(request: Request, data: EmailIn, db: DbSession) -> MessageOut:
    await auth_service.forgot_password(db, data.email)
    return MessageOut(message=auth_service.GENERIC_EMAIL_SENT)


@router.post("/reset-password", response_model=MessageOut)
@limiter.limit("10/minute")
async def reset_password(request: Request, data: ResetPasswordIn, db: DbSession) -> MessageOut:
    await auth_service.reset_password(
        db, email=data.email, code=data.code, new_password=data.new_password
    )
    return MessageOut(message="Your password has been reset. Please sign in.")


@router.post("/accept-invite", response_model=TokenPair)
@limiter.limit("10/minute")
async def accept_invite(request: Request, data: AcceptInviteIn, db: DbSession) -> TokenPair:
    """Staff invite: set a password with the token from the invite email. Signs in on success."""
    return await auth_service.accept_invite(
        db, token=data.token, password=data.password, device_info=_device(request)
    )


@router.post("/change-password", response_model=MessageOut)
@limiter.limit("5/minute")
async def change_password(
    request: Request, data: ChangePasswordIn, user: CurrentUser, db: DbSession
) -> MessageOut:
    """Signed-in password change. Signs out all other sessions."""
    await auth_service.change_password(
        db, user, current_password=data.current_password, new_password=data.new_password
    )
    return MessageOut(message="Password changed. Please sign in again on your other devices.")
