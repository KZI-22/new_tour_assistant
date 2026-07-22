from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from jwt import InvalidTokenError

ACCESS_COOKIE_NAME = "ta_access"
REFRESH_COOKIE_NAME = "ta_refresh"
CSRF_COOKIE_NAME = "ta_csrf"


class AccessTokenError(ValueError):
    """Raised when an access token is malformed or cannot be trusted."""


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    token_id: uuid.UUID


class JwtCodec:
    def __init__(
        self,
        secret: str,
        *,
        issuer: str,
        audience: str,
        access_token_minutes: int,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("JWT secret must contain at least 32 characters.")
        if access_token_minutes <= 0:
            raise ValueError("Access token lifetime must be positive.")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience
        self._access_token_lifetime = timedelta(minutes=access_token_minutes)

    @property
    def access_token_seconds(self) -> int:
        return int(self._access_token_lifetime.total_seconds())

    def create_access_token(
        self,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> str:
        issued_at = now or datetime.now(UTC)
        payload = {
            "sub": str(user_id),
            "sid": str(session_id),
            "jti": str(uuid.uuid4()),
            "type": "access",
            "iss": self._issuer,
            "aud": self._audience,
            "iat": issued_at,
            "nbf": issued_at,
            "exp": issued_at + self._access_token_lifetime,
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def decode_access_token(self, token: str) -> AccessTokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["sub", "sid", "jti", "type", "iss", "aud", "iat", "exp"]
                },
            )
            if payload.get("type") != "access":
                raise AccessTokenError("Unexpected token type.")
            return AccessTokenClaims(
                user_id=uuid.UUID(str(payload["sub"])),
                session_id=uuid.UUID(str(payload["sid"])),
                token_id=uuid.UUID(str(payload["jti"])),
            )
        except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
            raise AccessTokenError("Invalid access token.") from exc


def generate_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def keyed_digest(secret: str, purpose: str, value: str) -> str:
    message = f"{purpose}\0{value}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def secrets_match(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


__all__ = [
    "ACCESS_COOKIE_NAME",
    "AccessTokenClaims",
    "AccessTokenError",
    "CSRF_COOKIE_NAME",
    "JwtCodec",
    "REFRESH_COOKIE_NAME",
    "generate_opaque_token",
    "hash_token",
    "keyed_digest",
    "secrets_match",
]
