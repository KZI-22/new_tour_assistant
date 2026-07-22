from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.dependencies import get_auth_service, require_current_user
from app.core.security import (
    ACCESS_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    secrets_match,
)
from app.schemas.auth import (
    AuthStateResponse,
    AuthUserResponse,
    LoginResponse,
    PhoneLoginRequest,
    SendSmsCodeRequest,
    SendSmsCodeResponse,
)
from app.services.auth_service import (
    AuthenticatedUser,
    AuthenticationError,
    AuthService,
    CsrfValidationError,
    PhoneLoginResult,
    SessionResult,
    UserDisabledError,
)
from app.services.otp_service import (
    InvalidOtpError,
    InvalidPhoneError,
    OtpRateLimitedError,
    OtpService,
    mask_phone,
)
from app.services.otp_store import OtpStoreUnavailableError

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


def _otp_service(request: Request) -> OtpService:
    service = request.app.state.otp_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured.",
        )
    return service


def _auth_service(request: Request) -> AuthService:
    return get_auth_service(request)


def _client_ip(request: Request) -> str | None:
    context = getattr(request.state, "travel_context", None)
    return context.client_ip if context is not None else None


def _validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        if request.app.state.settings.app_environment == "production":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Request origin is not allowed.",
            )
        return
    if origin not in request.app.state.settings.cors_origins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request origin is not allowed.",
        )


def _csrf_token(request: Request) -> str:
    header_token = request.headers.get("x-csrf-token")
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not header_token or not cookie_token or not secrets_match(header_token, cookie_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed.",
        )
    return header_token


def _set_session_cookies(
    request: Request,
    response: Response,
    result: SessionResult | PhoneLoginResult,
) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        result.access_token,
        max_age=result.access_expires_in,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        result.refresh_token,
        max_age=result.refresh_expires_in,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        result.csrf_token,
        max_age=result.refresh_expires_in,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def _clear_session_cookies(request: Request, response: Response) -> None:
    secure = request.app.state.settings.auth_cookie_secure
    response.delete_cookie(
        ACCESS_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"


@router.post(
    "/sms-codes",
    response_model=SendSmsCodeResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_sms_code(
    payload: SendSmsCodeRequest,
    request: Request,
    response: Response,
) -> SendSmsCodeResponse:
    _validate_origin(request)
    try:
        challenge = await _otp_service(request).send_code(
            payload.phone,
            client_ip=_client_ip(request),
        )
    except InvalidPhoneError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except OtpRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="验证码请求过于频繁，请稍后重试。",
            headers={"Retry-After": str(max(exc.retry_after, 1))},
        ) from exc
    except OtpStoreUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="验证码服务暂时不可用，请稍后重试。",
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    return SendSmsCodeResponse(
        challenge_id=challenge.challenge_id,
        expires_in=challenge.expires_in,
        resend_after=challenge.resend_after,
        debug_code=challenge.debug_code,
    )


@router.post("/phone-login", response_model=LoginResponse)
async def phone_login(
    payload: PhoneLoginRequest,
    request: Request,
    response: Response,
) -> LoginResponse:
    _validate_origin(request)
    try:
        phone_e164 = await _otp_service(request).verify_code(
            payload.challenge_id,
            payload.phone,
            payload.code,
        )
        result = await _auth_service(request).login_phone(
            phone_e164,
            user_agent=request.headers.get("user-agent"),
            ip_address=_client_ip(request),
        )
    except (InvalidPhoneError, InvalidOtpError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="验证码无效或已过期，请重新获取。",
        ) from exc
    except OtpStoreUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="验证码服务暂时不可用，请稍后重试。",
        ) from exc
    except UserDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该账号当前不可用。",
        ) from exc
    _set_session_cookies(request, response, result)
    return LoginResponse(
        user=AuthUserResponse(
            id=result.user.id,
            phone=mask_phone(result.user.phone_e164),
            display_name=result.user.display_name,
        ),
        is_new_user=result.is_new_user,
        access_expires_in=result.access_expires_in,
    )


@router.get("/me", response_model=AuthUserResponse)
async def current_user(
    response: Response,
    user: Annotated[AuthenticatedUser, Depends(require_current_user)],
) -> AuthUserResponse:
    response.headers["Cache-Control"] = "no-store"
    return AuthUserResponse(
        id=user.id,
        phone=mask_phone(user.phone_e164),
        display_name=user.display_name,
    )


@router.post("/refresh", response_model=AuthStateResponse)
async def refresh_session(request: Request, response: Response) -> AuthStateResponse:
    _validate_origin(request)
    csrf_token = _csrf_token(request)
    try:
        result = await _auth_service(request).refresh_session(
            request.cookies.get(REFRESH_COOKIE_NAME),
            csrf_token,
            user_agent=request.headers.get("user-agent"),
            ip_address=_client_ip(request),
        )
    except CsrfValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed.",
        ) from exc
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login session is invalid or expired.",
        ) from exc
    _set_session_cookies(request, response, result)
    return AuthStateResponse(
        user=AuthUserResponse(
            id=result.user.id,
            phone=mask_phone(result.user.phone_e164),
            display_name=result.user.display_name,
        ),
        access_expires_in=result.access_expires_in,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_session(request: Request, response: Response) -> None:
    _validate_origin(request)
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    csrf_token = _csrf_token(request) if refresh_token else None
    try:
        await _auth_service(request).logout_session(refresh_token, csrf_token)
    except CsrfValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed.",
        ) from exc
    _clear_session_cookies(request, response)


__all__ = ["router"]
