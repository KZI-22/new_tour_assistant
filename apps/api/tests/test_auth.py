from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.core.security import AccessTokenError, JwtCodec
from app.core.settings import Settings
from app.main import create_app
from app.services.auth_service import (
    AuthenticatedUser,
    AuthenticationError,
    LoginUser,
    PhoneLoginResult,
    SessionResult,
)
from app.services.otp_service import (
    InvalidOtpError,
    InvalidPhoneError,
    OtpService,
    SentOtpChallenge,
    normalize_china_phone,
)
from app.services.otp_store import OtpChallengeRecord, OtpCreateResult
from fastapi.testclient import TestClient

_JWT_SECRET = "jwt-test-secret-with-at-least-thirty-two-characters"
_HMAC_SECRET = "hmac-test-secret-with-at-least-thirty-two-characters"


class FakeOtpStore:
    def __init__(self) -> None:
        self.records: dict[str, OtpChallengeRecord] = {}
        self.next_result = OtpCreateResult("created")

    async def create(
        self,
        challenge_id: str,
        *,
        phone_digest: str,
        code_digest: str,
        **_: object,
    ) -> OtpCreateResult:
        if self.next_result.status == "created":
            self.records[challenge_id] = OtpChallengeRecord(
                phone_digest=phone_digest,
                code_digest=code_digest,
                attempts=0,
            )
        return self.next_result

    async def get(self, challenge_id: str) -> OtpChallengeRecord | None:
        return self.records.get(challenge_id)

    async def record_failure(self, challenge_id: str, *, max_attempts: int) -> None:
        record = self.records.get(challenge_id)
        if record is None:
            return
        attempts = record.attempts + 1
        if attempts >= max_attempts:
            self.records.pop(challenge_id, None)
            return
        self.records[challenge_id] = OtpChallengeRecord(
            phone_digest=record.phone_digest,
            code_digest=record.code_digest,
            attempts=attempts,
        )

    async def consume(
        self,
        challenge_id: str,
        *,
        phone_digest: str,
        code_digest: str,
    ) -> bool:
        record = self.records.get(challenge_id)
        if record is None:
            return False
        if record.phone_digest != phone_digest or record.code_digest != code_digest:
            return False
        self.records.pop(challenge_id)
        return True


def test_normalize_china_phone() -> None:
    assert normalize_china_phone("13812345678") == "+8613812345678"
    assert normalize_china_phone("+86 138-1234-5678") == "+8613812345678"
    with pytest.raises(InvalidPhoneError):
        normalize_china_phone("12345")


@pytest.mark.asyncio
async def test_otp_is_random_shaped_and_consumed_once() -> None:
    store = FakeOtpStore()
    service = OtpService(
        store,
        hmac_secret=_HMAC_SECRET,
        expose_debug_code=True,
    )

    sent = await service.send_code("13812345678", client_ip="127.0.0.1")

    assert sent.debug_code is not None
    assert re.fullmatch(r"\d{6}", sent.debug_code)
    assert await service.verify_code(sent.challenge_id, "13812345678", sent.debug_code) == (
        "+8613812345678"
    )
    with pytest.raises(InvalidOtpError):
        await service.verify_code(sent.challenge_id, "13812345678", sent.debug_code)


@pytest.mark.asyncio
async def test_otp_rejects_mismatch_and_locks_after_max_attempts() -> None:
    store = FakeOtpStore()
    service = OtpService(
        store,
        hmac_secret=_HMAC_SECRET,
        max_attempts=2,
        expose_debug_code=True,
    )
    sent = await service.send_code("13812345678", client_ip=None)
    wrong_code = "000000" if sent.debug_code != "000000" else "000001"

    for _ in range(2):
        with pytest.raises(InvalidOtpError, match="验证码无效"):
            await service.verify_code(sent.challenge_id, "13812345678", wrong_code)

    assert str(sent.challenge_id) not in store.records


def test_jwt_codec_validates_expiry_and_session_binding() -> None:
    codec = JwtCodec(
        _JWT_SECRET,
        issuer="test-issuer",
        audience="test-audience",
        access_token_minutes=15,
    )
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()

    claims = codec.decode_access_token(codec.create_access_token(user_id, session_id))

    assert claims.user_id == user_id
    assert claims.session_id == session_id
    expired = codec.create_access_token(
        user_id,
        session_id,
        now=datetime.now(UTC) - timedelta(minutes=16),
    )
    with pytest.raises(AccessTokenError):
        codec.decode_access_token(expired)


def _test_settings(tmp_path: Path) -> Settings:
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        "default_model: null\nmodels: []\n",
        encoding="utf-8",
    )
    return Settings(
        app_name="Auth Test API",
        model_config_path=config_path,
        cors_origins=("http://localhost:3000",),
        log_level="WARNING",
    )


def test_phone_login_sets_private_cookies(tmp_path: Path) -> None:
    client = TestClient(create_app(_test_settings(tmp_path)))
    challenge_id = uuid.uuid4()

    class FakeOtpService:
        async def verify_code(self, *_: object) -> str:
            return "+8613812345678"

    class FakeAuthService:
        async def login_phone(self, *_: object, **__: object) -> PhoneLoginResult:
            return PhoneLoginResult(
                user=LoginUser(
                    id=uuid.uuid4(),
                    phone_e164="+8613812345678",
                    display_name=None,
                ),
                is_new_user=True,
                access_token="access-token",
                refresh_token="refresh-token",
                csrf_token="csrf-token",
                access_expires_in=900,
                refresh_expires_in=2_592_000,
            )

    client.app.state.otp_service = FakeOtpService()
    client.app.state.auth_service = FakeAuthService()
    response = client.post(
        "/api/v1/auth/phone-login",
        json={
            "challenge_id": str(challenge_id),
            "phone": "13812345678",
            "code": "123456",
        },
    )

    assert response.status_code == 200
    assert response.json()["user"]["phone"] == "138****5678"
    assert response.json()["is_new_user"] is True
    assert response.headers["cache-control"] == "no-store"
    cookie_headers = response.headers.get_list("set-cookie")
    assert any("ta_access=access-token" in item and "HttpOnly" in item for item in cookie_headers)
    assert any("ta_refresh=refresh-token" in item and "HttpOnly" in item for item in cookie_headers)
    assert any("ta_csrf=csrf-token" in item and "HttpOnly" not in item for item in cookie_headers)


def test_send_sms_code_returns_debug_code_only_when_service_exposes_it(tmp_path: Path) -> None:
    client = TestClient(create_app(_test_settings(tmp_path)))

    class FakeOtpService:
        async def send_code(self, *_: object, **__: object) -> SentOtpChallenge:
            return SentOtpChallenge(
                challenge_id=uuid.uuid4(),
                expires_in=300,
                resend_after=60,
                debug_code=None,
            )

    client.app.state.otp_service = FakeOtpService()
    response = client.post("/api/v1/auth/sms-codes", json={"phone": "13812345678"})

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert "debug_code" not in response.json()


def test_me_refresh_and_logout_use_cookie_backed_session(tmp_path: Path) -> None:
    client = TestClient(create_app(_test_settings(tmp_path)))
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()

    class FakeAuthService:
        logout_args: tuple[str | None, str | None] | None = None

        async def authenticate_access(self, token: str | None) -> AuthenticatedUser:
            if token != "old-access":
                raise AuthenticationError("invalid")
            return AuthenticatedUser(
                id=user_id,
                phone_e164="+8613812345678",
                display_name="旅行者",
                session_id=session_id,
            )

        async def refresh_session(self, *_: object, **__: object) -> SessionResult:
            return SessionResult(
                user=LoginUser(
                    id=user_id,
                    phone_e164="+8613812345678",
                    display_name="旅行者",
                ),
                access_token="new-access",
                refresh_token="new-refresh",
                csrf_token="new-csrf",
                access_expires_in=900,
                refresh_expires_in=2_592_000,
            )

        async def logout_session(
            self,
            refresh_token: str | None,
            csrf_token: str | None,
        ) -> None:
            self.logout_args = (refresh_token, csrf_token)

    auth_service = FakeAuthService()
    client.app.state.auth_service = auth_service
    client.cookies.set("ta_access", "old-access", domain="testserver.local", path="/")
    me_response = client.get("/api/v1/auth/me")

    assert me_response.status_code == 200
    assert me_response.json() == {
        "id": str(user_id),
        "phone": "138****5678",
        "display_name": "旅行者",
    }
    client.cookies.delete("ta_access", domain="testserver.local", path="/")
    assert client.get("/api/v1/auth/me").status_code == 401
    client.cookies.set("ta_access", "old-access", domain="testserver.local", path="/")

    client.cookies.set("ta_refresh", "old-refresh", domain="testserver.local", path="/")
    client.cookies.set("ta_csrf", "old-csrf", domain="testserver.local", path="/")
    refresh_response = client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": "old-csrf"},
    )

    assert refresh_response.status_code == 200
    assert refresh_response.json()["access_expires_in"] == 900
    assert client.cookies.get("ta_access") == "new-access"
    assert client.cookies.get("ta_refresh") == "new-refresh"
    assert client.cookies.get("ta_csrf") == "new-csrf"

    logout_response = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": "new-csrf"},
    )

    assert logout_response.status_code == 204
    assert auth_service.logout_args == ("new-refresh", "new-csrf")
    assert client.cookies.get("ta_access") is None
    assert client.cookies.get("ta_refresh") is None
    assert client.cookies.get("ta_csrf") is None


def test_refresh_rejects_cross_origin_and_csrf_mismatch(tmp_path: Path) -> None:
    client = TestClient(create_app(_test_settings(tmp_path)))
    client.cookies.set("ta_refresh", "refresh", domain="testserver.local", path="/")
    client.cookies.set("ta_csrf", "csrf", domain="testserver.local", path="/")

    cross_origin = client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "https://evil.example", "X-CSRF-Token": "csrf"},
    )
    csrf_mismatch = client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": "wrong"},
    )

    assert cross_origin.status_code == 403
    assert csrf_mismatch.status_code == 403
