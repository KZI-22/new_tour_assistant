from __future__ import annotations

import os
import uuid

import pytest
from app.services.otp_service import InvalidOtpError, OtpRateLimitedError, OtpService
from app.services.otp_store import RedisOtpChallengeStore
from redis.asyncio import Redis

_HMAC_SECRET = "redis-test-hmac-secret-with-at-least-thirty-two-characters"


@pytest.mark.redis
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REDIS_TESTS") != "1",
    reason="Set RUN_REDIS_TESTS=1 to run Redis integration tests.",
)
async def test_redis_otp_is_rate_limited_and_consumed_once() -> None:
    client = Redis.from_url(
        os.getenv("REDIS_TEST_URL", "redis://localhost:6379/15"),
        decode_responses=True,
    )
    prefix = f"test:{uuid.uuid4()}:auth:otp"
    service = OtpService(
        RedisOtpChallengeStore(client, key_prefix=prefix),
        hmac_secret=_HMAC_SECRET,
        resend_seconds=60,
        max_attempts=2,
        expose_debug_code=True,
    )
    try:
        sent = await service.send_code("13812345678", client_ip="127.0.0.1")
        assert sent.debug_code is not None

        with pytest.raises(OtpRateLimitedError):
            await service.send_code("13812345678", client_ip="127.0.0.1")
        wrong_code = "000000" if sent.debug_code != "000000" else "000001"
        with pytest.raises(InvalidOtpError):
            await service.verify_code(sent.challenge_id, "13812345678", wrong_code)

        assert await service.verify_code(
            sent.challenge_id,
            "13812345678",
            sent.debug_code,
        ) == "+8613812345678"
        with pytest.raises(InvalidOtpError):
            await service.verify_code(sent.challenge_id, "13812345678", sent.debug_code)
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()
