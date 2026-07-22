from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass

from app.core.security import keyed_digest, secrets_match
from app.services.otp_store import OtpChallengeStore

_CHINA_PHONE = re.compile(r"1[3-9]\d{9}")
_SIX_DIGIT_CODE = re.compile(r"\d{6}")


class InvalidPhoneError(ValueError):
    """Raised when a phone number cannot be normalized for this release."""


class InvalidOtpError(ValueError):
    """Raised for every invalid, expired, consumed, or mismatched OTP."""


class OtpRateLimitedError(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("OTP request rate limit reached.")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class SentOtpChallenge:
    challenge_id: uuid.UUID
    expires_in: int
    resend_after: int
    debug_code: str | None


def normalize_china_phone(value: str) -> str:
    normalized = re.sub(r"[\s()-]", "", value.strip())
    if normalized.startswith("+86"):
        normalized = normalized[3:]
    elif normalized.startswith("86") and len(normalized) == 13:
        normalized = normalized[2:]
    if not _CHINA_PHONE.fullmatch(normalized):
        raise InvalidPhoneError("请输入有效的中国大陆手机号。")
    return f"+86{normalized}"


def mask_phone(phone_e164: str) -> str:
    local = phone_e164.removeprefix("+86")
    return f"{local[:3]}****{local[-4:]}"


class OtpService:
    def __init__(
        self,
        store: OtpChallengeStore,
        *,
        hmac_secret: str,
        ttl_seconds: int = 300,
        resend_seconds: int = 60,
        max_attempts: int = 5,
        phone_limit: int = 5,
        ip_limit: int = 20,
        rate_window_seconds: int = 600,
        expose_debug_code: bool = False,
    ) -> None:
        if len(hmac_secret) < 32:
            raise ValueError("OTP HMAC secret must contain at least 32 characters.")
        limits = (
            ttl_seconds,
            resend_seconds,
            max_attempts,
            phone_limit,
            ip_limit,
            rate_window_seconds,
        )
        if any(value <= 0 for value in limits):
            raise ValueError("OTP limits and timeouts must be positive.")
        self._store = store
        self._hmac_secret = hmac_secret
        self._ttl_seconds = ttl_seconds
        self._resend_seconds = resend_seconds
        self._max_attempts = max_attempts
        self._phone_limit = phone_limit
        self._ip_limit = ip_limit
        self._rate_window_seconds = rate_window_seconds
        self._expose_debug_code = expose_debug_code

    async def send_code(self, phone: str, *, client_ip: str | None) -> SentOtpChallenge:
        phone_e164 = normalize_china_phone(phone)
        challenge_id = uuid.uuid4()
        code = f"{secrets.randbelow(1_000_000):06d}"
        phone_digest = keyed_digest(self._hmac_secret, "otp-phone", phone_e164)
        ip_digest = keyed_digest(self._hmac_secret, "otp-ip", client_ip or "unknown")
        code_digest = keyed_digest(
            self._hmac_secret,
            f"otp-code:{challenge_id}",
            code,
        )
        result = await self._store.create(
            str(challenge_id),
            phone_digest=phone_digest,
            code_digest=code_digest,
            ip_digest=ip_digest,
            ttl_seconds=self._ttl_seconds,
            resend_seconds=self._resend_seconds,
            rate_window_seconds=self._rate_window_seconds,
            phone_limit=self._phone_limit,
            ip_limit=self._ip_limit,
        )
        if result.status != "created":
            raise OtpRateLimitedError(result.retry_after)
        return SentOtpChallenge(
            challenge_id=challenge_id,
            expires_in=self._ttl_seconds,
            resend_after=self._resend_seconds,
            debug_code=code if self._expose_debug_code else None,
        )

    async def verify_code(self, challenge_id: uuid.UUID, phone: str, code: str) -> str:
        phone_e164 = normalize_china_phone(phone)
        if not _SIX_DIGIT_CODE.fullmatch(code):
            raise InvalidOtpError("验证码无效或已过期，请重新获取。")
        record = await self._store.get(str(challenge_id))
        if record is None:
            raise InvalidOtpError("验证码无效或已过期，请重新获取。")
        phone_digest = keyed_digest(self._hmac_secret, "otp-phone", phone_e164)
        code_digest = keyed_digest(
            self._hmac_secret,
            f"otp-code:{challenge_id}",
            code,
        )
        matches = secrets_match(record.phone_digest, phone_digest) and secrets_match(
            record.code_digest, code_digest
        )
        if not matches:
            await self._store.record_failure(
                str(challenge_id),
                max_attempts=self._max_attempts,
            )
            raise InvalidOtpError("验证码无效或已过期，请重新获取。")
        consumed = await self._store.consume(
            str(challenge_id),
            phone_digest=phone_digest,
            code_digest=code_digest,
        )
        if not consumed:
            raise InvalidOtpError("验证码无效或已过期，请重新获取。")
        return phone_e164


__all__ = [
    "InvalidOtpError",
    "InvalidPhoneError",
    "OtpRateLimitedError",
    "OtpService",
    "SentOtpChallenge",
    "mask_phone",
    "normalize_china_phone",
]
