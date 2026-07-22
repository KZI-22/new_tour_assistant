from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import (
    AccessTokenError,
    JwtCodec,
    generate_opaque_token,
    hash_token,
    secrets_match,
)
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.user_repository import UserRepository


class UserDisabledError(PermissionError):
    """Raised when a disabled account attempts to establish a new session."""


class AuthenticationError(PermissionError):
    """Raised when a token or its backing server-side session is invalid."""


class CsrfValidationError(PermissionError):
    """Raised when a refresh-session operation has an invalid CSRF token."""


@dataclass(frozen=True, slots=True)
class LoginUser:
    id: uuid.UUID
    phone_e164: str
    display_name: str | None


@dataclass(frozen=True, slots=True)
class AuthenticatedUser(LoginUser):
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class SessionResult:
    user: LoginUser
    access_token: str
    refresh_token: str
    csrf_token: str
    access_expires_in: int
    refresh_expires_in: int


@dataclass(frozen=True, slots=True)
class PhoneLoginResult(SessionResult):
    is_new_user: bool


class AuthService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        jwt_codec: JwtCodec,
        *,
        refresh_token_days: int = 30,
        users: UserRepository | None = None,
        auth_sessions: AuthSessionRepository | None = None,
    ) -> None:
        if refresh_token_days <= 0:
            raise ValueError("Refresh token lifetime must be positive.")
        self._session_factory = session_factory
        self._jwt_codec = jwt_codec
        self._refresh_lifetime = timedelta(days=refresh_token_days)
        self._users = users or UserRepository()
        self._auth_sessions = auth_sessions or AuthSessionRepository()

    async def login_phone(
        self,
        phone_e164: str,
        *,
        user_agent: str | None,
        ip_address: str | None,
    ) -> PhoneLoginResult:
        now = datetime.now(UTC)
        refresh_token = generate_opaque_token()
        csrf_token = generate_opaque_token()
        async with self._session_factory() as session, session.begin():
            user, is_new_user = await self._users.get_or_create_verified(
                session,
                phone_e164,
                now=now,
            )
            if user.status != "active":
                raise UserDisabledError("User account is disabled.")
            user.phone_verified_at = now
            user.last_login_at = now
            user.updated_at = now
            auth_session = await self._auth_sessions.create(
                session,
                user_id=user.id,
                refresh_token_hash=hash_token(refresh_token),
                csrf_token_hash=hash_token(csrf_token),
                expires_at=now + self._refresh_lifetime,
                now=now,
                user_agent=(user_agent or "")[:500] or None,
                ip_address=(ip_address or "")[:64] or None,
            )
            access_token = self._jwt_codec.create_access_token(user.id, auth_session.id, now=now)
            result_user = LoginUser(
                id=user.id,
                phone_e164=user.phone_e164,
                display_name=user.display_name,
            )
        return PhoneLoginResult(
            user=result_user,
            access_token=access_token,
            refresh_token=refresh_token,
            csrf_token=csrf_token,
            access_expires_in=self._jwt_codec.access_token_seconds,
            refresh_expires_in=int(self._refresh_lifetime.total_seconds()),
            is_new_user=is_new_user,
        )

    async def authenticate_access(self, access_token: str | None) -> AuthenticatedUser:
        if not access_token:
            raise AuthenticationError("Access token is missing.")
        try:
            claims = self._jwt_codec.decode_access_token(access_token)
        except AccessTokenError as exc:
            raise AuthenticationError("Access token is invalid.") from exc
        async with self._session_factory() as session:
            auth_session = await self._auth_sessions.get_for_access(
                session,
                session_id=claims.session_id,
                user_id=claims.user_id,
                now=datetime.now(UTC),
            )
            if auth_session is None or auth_session.user.status != "active":
                raise AuthenticationError("Authentication session is invalid.")
            return AuthenticatedUser(
                id=auth_session.user.id,
                phone_e164=auth_session.user.phone_e164,
                display_name=auth_session.user.display_name,
                session_id=auth_session.id,
            )

    async def refresh_session(
        self,
        refresh_token: str | None,
        csrf_token: str | None,
        *,
        user_agent: str | None,
        ip_address: str | None,
    ) -> SessionResult:
        if not refresh_token:
            raise AuthenticationError("Refresh token is missing.")
        if not csrf_token:
            raise CsrfValidationError("CSRF token is missing.")
        now = datetime.now(UTC)
        new_refresh_token = generate_opaque_token()
        new_csrf_token = generate_opaque_token()
        async with self._session_factory() as session, session.begin():
            auth_session = await self._auth_sessions.get_by_refresh_hash_for_update(
                session,
                hash_token(refresh_token),
            )
            if (
                auth_session is None
                or auth_session.revoked_at is not None
                or auth_session.expires_at <= now
                or auth_session.user.status != "active"
            ):
                raise AuthenticationError("Refresh session is invalid.")
            if not secrets_match(auth_session.csrf_token_hash, hash_token(csrf_token)):
                raise CsrfValidationError("CSRF token is invalid.")
            auth_session.refresh_token_hash = hash_token(new_refresh_token)
            auth_session.csrf_token_hash = hash_token(new_csrf_token)
            auth_session.last_used_at = now
            auth_session.user_agent = (user_agent or "")[:500] or None
            auth_session.ip_address = (ip_address or "")[:64] or None
            access_token = self._jwt_codec.create_access_token(
                auth_session.user.id,
                auth_session.id,
                now=now,
            )
            result_user = LoginUser(
                id=auth_session.user.id,
                phone_e164=auth_session.user.phone_e164,
                display_name=auth_session.user.display_name,
            )
        return SessionResult(
            user=result_user,
            access_token=access_token,
            refresh_token=new_refresh_token,
            csrf_token=new_csrf_token,
            access_expires_in=self._jwt_codec.access_token_seconds,
            refresh_expires_in=max(int((auth_session.expires_at - now).total_seconds()), 1),
        )

    async def logout_session(
        self,
        refresh_token: str | None,
        csrf_token: str | None,
    ) -> None:
        if not refresh_token:
            return
        if not csrf_token:
            raise CsrfValidationError("CSRF token is missing.")
        async with self._session_factory() as session, session.begin():
            auth_session = await self._auth_sessions.get_by_refresh_hash_for_update(
                session,
                hash_token(refresh_token),
            )
            if auth_session is None or auth_session.revoked_at is not None:
                return
            if not secrets_match(auth_session.csrf_token_hash, hash_token(csrf_token)):
                raise CsrfValidationError("CSRF token is invalid.")
            auth_session.revoked_at = datetime.now(UTC)


__all__ = [
    "AuthenticatedUser",
    "AuthenticationError",
    "AuthService",
    "CsrfValidationError",
    "LoginUser",
    "PhoneLoginResult",
    "SessionResult",
    "UserDisabledError",
]
