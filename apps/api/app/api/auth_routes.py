from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.schemas.auth import (
    AuthUserResponse,
    LoginResponse,
    PhoneLoginRequest,
    SendSmsCodeRequest,
    SendSmsCodeResponse,
)
from app.services.auth_service import AuthService, PhoneLoginResult, UserDisabledError
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
    service = request.app.state.auth_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured.",
        )
    return service


def _client_ip(request: Request) -> str | None:
    context = getattr(request.state, "travel_context", None)
    return context.client_ip if context is not None else None


def _set_login_cookies(
    request: Request,
    response: Response,
    result: PhoneLoginResult,
) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        "ta_access",
        result.access_token,
        max_age=result.access_expires_in,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        "ta_refresh",
        result.refresh_token,
        max_age=result.refresh_expires_in,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        "ta_csrf",
        result.csrf_token,
        max_age=result.refresh_expires_in,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
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
    _set_login_cookies(request, response, result)
    return LoginResponse(
        user=AuthUserResponse(
            id=result.user.id,
            phone=mask_phone(result.user.phone_e164),
            display_name=result.user.display_name,
        ),
        is_new_user=result.is_new_user,
        access_expires_in=result.access_expires_in,
    )


__all__ = ["router"]
