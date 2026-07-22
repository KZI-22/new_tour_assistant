from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.core.security import ACCESS_COOKIE_NAME
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


__all__ = ["get_auth_service", "require_current_user"]
