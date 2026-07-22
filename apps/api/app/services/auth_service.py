from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import JwtCodec, generate_opaque_token, hash_token
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.user_repository import UserRepository


class UserDisabledError(PermissionError):
    """Raised when a disabled account attempts to establish a new session."""


@dataclass(frozen=True, slots=True)
class LoginUser:
    id: uuid.UUID
    phone_e164: str
    display_name: str | None


@dataclass(frozen=True, slots=True)
class PhoneLoginResult:
    user: LoginUser
    is_new_user: bool
    access_token: str
    refresh_token: str
    csrf_token: str
    access_expires_in: int
    refresh_expires_in: int


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
            is_new_user=is_new_user,
            access_token=access_token,
            refresh_token=refresh_token,
            csrf_token=csrf_token,
            access_expires_in=self._jwt_codec.access_token_seconds,
            refresh_expires_in=int(self._refresh_lifetime.total_seconds()),
        )


__all__ = [
    "AuthService",
    "LoginUser",
    "PhoneLoginResult",
    "UserDisabledError",
]
