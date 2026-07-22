from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.core.security import ACCESS_COOKIE_NAME, CSRF_COOKIE_NAME, secrets_match
from app.services.auth_service import AuthenticatedUser, AuthenticationError, AuthService


def get_auth_service(request: Request) -> AuthService:
    service = request.app.state.auth_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured.",
        )
    return service


async def require_current_user(request: Request) -> AuthenticatedUser:
    try:
        return await get_auth_service(request).authenticate_access(
            request.cookies.get(ACCESS_COOKIE_NAME)
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Cookie"},
        ) from exc


def validate_request_origin(request: Request) -> None:
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


def require_csrf_token(request: Request) -> str:
    header_token = request.headers.get("x-csrf-token")
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not header_token or not cookie_token or not secrets_match(header_token, cookie_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed.",
        )
    return header_token


def require_csrf_protection(request: Request) -> str:
    validate_request_origin(request)
    return require_csrf_token(request)


__all__ = [
    "get_auth_service",
    "require_csrf_protection",
    "require_csrf_token",
    "require_current_user",
    "validate_request_origin",
]
