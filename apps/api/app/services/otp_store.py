from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError


class OtpStoreUnavailableError(RuntimeError):
    """Raised when the OTP store cannot safely process a request."""


@dataclass(frozen=True, slots=True)
class OtpChallengeRecord:
    phone_digest: str
    code_digest: str
    attempts: int


@dataclass(frozen=True, slots=True)
class OtpCreateResult:
    status: Literal["created", "cooldown", "phone_limited", "ip_limited"]
    retry_after: int = 0


class OtpChallengeStore(Protocol):
    async def create(
        self,
        challenge_id: str,
        *,
        phone_digest: str,
        code_digest: str,
        ip_digest: str,
        ttl_seconds: int,
        resend_seconds: int,
        rate_window_seconds: int,
        phone_limit: int,
        ip_limit: int,
    ) -> OtpCreateResult: ...

    async def get(self, challenge_id: str) -> OtpChallengeRecord | None: ...

    async def record_failure(self, challenge_id: str, *, max_attempts: int) -> None: ...

    async def consume(
        self,
        challenge_id: str,
        *,
        phone_digest: str,
        code_digest: str,
    ) -> bool: ...


_CREATE_CHALLENGE_SCRIPT = """
local cooldown_ttl = redis.call('TTL', KEYS[1])
if cooldown_ttl > 0 then
  return {'cooldown', tostring(cooldown_ttl)}
end

local phone_count = tonumber(redis.call('GET', KEYS[2]) or '0')
if phone_count >= tonumber(ARGV[4]) then
  return {'phone_limited', tostring(math.max(redis.call('TTL', KEYS[2]), 1))}
end

local ip_count = tonumber(redis.call('GET', KEYS[3]) or '0')
if ip_count >= tonumber(ARGV[5]) then
  return {'ip_limited', tostring(math.max(redis.call('TTL', KEYS[3]), 1))}
end

local new_phone_count = redis.call('INCR', KEYS[2])
if new_phone_count == 1 then redis.call('EXPIRE', KEYS[2], ARGV[3]) end
local new_ip_count = redis.call('INCR', KEYS[3])
if new_ip_count == 1 then redis.call('EXPIRE', KEYS[3], ARGV[3]) end

redis.call('SET', KEYS[1], '1', 'EX', ARGV[2])
redis.call(
  'HSET', KEYS[4],
  'phone_digest', ARGV[6],
  'code_digest', ARGV[7],
  'attempts', '0'
)
redis.call('EXPIRE', KEYS[4], ARGV[1])
return {'created', '0'}
"""

_RECORD_FAILURE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
local attempts = redis.call('HINCRBY', KEYS[1], 'attempts', 1)
if attempts >= tonumber(ARGV[1]) then redis.call('DEL', KEYS[1]) end
return attempts
"""

_CONSUME_CHALLENGE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
if redis.call('HGET', KEYS[1], 'phone_digest') ~= ARGV[1] then return 0 end
if redis.call('HGET', KEYS[1], 'code_digest') ~= ARGV[2] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


class RedisOtpChallengeStore:
    def __init__(self, client: Redis, *, key_prefix: str = "auth:otp") -> None:
        if not key_prefix or any(character.isspace() for character in key_prefix):
            raise ValueError("OTP key prefix must be non-empty and contain no whitespace.")
        self._client = client
        self._key_prefix = key_prefix.rstrip(":")

    def _key(self, suffix: str) -> str:
        return f"{self._key_prefix}:{suffix}"

    async def create(
        self,
        challenge_id: str,
        *,
        phone_digest: str,
        code_digest: str,
        ip_digest: str,
        ttl_seconds: int,
        resend_seconds: int,
        rate_window_seconds: int,
        phone_limit: int,
        ip_limit: int,
    ) -> OtpCreateResult:
        try:
            raw = await self._client.eval(
                _CREATE_CHALLENGE_SCRIPT,
                4,
                self._key(f"cooldown:{phone_digest}"),
                self._key(f"phone-rate:{phone_digest}"),
                self._key(f"ip-rate:{ip_digest}"),
                self._key(f"challenge:{challenge_id}"),
                ttl_seconds,
                resend_seconds,
                rate_window_seconds,
                phone_limit,
                ip_limit,
                phone_digest,
                code_digest,
            )
        except RedisError as exc:
            raise OtpStoreUnavailableError("OTP store is unavailable.") from exc
        status = str(raw[0])
        retry_after = int(raw[1])
        if status not in {"created", "cooldown", "phone_limited", "ip_limited"}:
            raise OtpStoreUnavailableError("OTP store returned an unexpected result.")
        return OtpCreateResult(status=status, retry_after=retry_after)  # type: ignore[arg-type]

    async def get(self, challenge_id: str) -> OtpChallengeRecord | None:
        try:
            raw = await self._client.hgetall(self._key(f"challenge:{challenge_id}"))
        except RedisError as exc:
            raise OtpStoreUnavailableError("OTP store is unavailable.") from exc
        if not raw:
            return None
        try:
            return OtpChallengeRecord(
                phone_digest=str(raw["phone_digest"]),
                code_digest=str(raw["code_digest"]),
                attempts=int(raw["attempts"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OtpStoreUnavailableError("OTP challenge data is invalid.") from exc

    async def record_failure(self, challenge_id: str, *, max_attempts: int) -> None:
        try:
            await self._client.eval(
                _RECORD_FAILURE_SCRIPT,
                1,
                self._key(f"challenge:{challenge_id}"),
                max_attempts,
            )
        except RedisError as exc:
            raise OtpStoreUnavailableError("OTP store is unavailable.") from exc

    async def consume(
        self,
        challenge_id: str,
        *,
        phone_digest: str,
        code_digest: str,
    ) -> bool:
        try:
            consumed = await self._client.eval(
                _CONSUME_CHALLENGE_SCRIPT,
                1,
                self._key(f"challenge:{challenge_id}"),
                phone_digest,
                code_digest,
            )
        except RedisError as exc:
            raise OtpStoreUnavailableError("OTP store is unavailable.") from exc
        return consumed == 1


__all__ = [
    "OtpChallengeRecord",
    "OtpChallengeStore",
    "OtpCreateResult",
    "OtpStoreUnavailableError",
    "RedisOtpChallengeStore",
]
