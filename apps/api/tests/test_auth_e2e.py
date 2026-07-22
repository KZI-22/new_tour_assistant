from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from app.core.settings import PROJECT_ROOT, Settings
from app.db.models import User
from app.db.session import create_database
from app.main import create_app
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import delete

_JWT_SECRET = "e2e-jwt-test-secret-with-at-least-thirty-two-characters"
_HMAC_SECRET = "e2e-hmac-test-secret-with-at-least-thirty-two-characters"


@pytest.mark.database
@pytest.mark.redis
@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_TESTS") != "1" or os.getenv("RUN_REDIS_TESTS") != "1",
    reason="Set RUN_DATABASE_TESTS=1 and RUN_REDIS_TESTS=1 to run auth E2E tests.",
)
def test_real_otp_login_me_and_logout(tmp_path: Path) -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    model_config = tmp_path / "models.yaml"
    model_config.write_text("default_model: null\nmodels: []\n", encoding="utf-8")
    settings = Settings(
        app_name="Auth E2E",
        model_config_path=model_config,
        cors_origins=("http://localhost:3000",),
        log_level="WARNING",
        database_url=os.environ["DATABASE_URL"],
        app_environment="test",
        auth_enabled=True,
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        auth_jwt_secret=_JWT_SECRET,
        auth_hmac_secret=_HMAC_SECRET,
        trip_planner_enabled=False,
    )
    phone = f"139{uuid.uuid4().int % 100_000_000:08d}"
    user_id: uuid.UUID | None = None
    headers = {"Origin": "http://localhost:3000"}

    try:
        with TestClient(create_app(settings)) as client:
            code_response = client.post(
                "/api/v1/auth/sms-codes",
                json={"phone": phone},
                headers=headers,
            )
            assert code_response.status_code == 202
            body = code_response.json()
            assert body["debug_code"]

            login_response = client.post(
                "/api/v1/auth/phone-login",
                json={
                    "challenge_id": body["challenge_id"],
                    "phone": phone,
                    "code": body["debug_code"],
                },
                headers=headers,
            )
            assert login_response.status_code == 200
            user_id = uuid.UUID(login_response.json()["user"]["id"])
            assert login_response.json()["is_new_user"] is True
            assert client.get("/api/v1/auth/me").status_code == 200

            csrf_token = client.cookies.get("ta_csrf")
            logout_response = client.post(
                "/api/v1/auth/logout",
                headers={**headers, "X-CSRF-Token": csrf_token or ""},
            )
            assert logout_response.status_code == 204
            assert client.get("/api/v1/auth/me").status_code == 401
    finally:
        if user_id is not None:
            asyncio.run(_delete_user(settings.database_url or "", user_id))


async def _delete_user(database_url: str, user_id: uuid.UUID) -> None:
    engine, session_factory = create_database(database_url)
    try:
        async with session_factory() as session, session.begin():
            await session.execute(delete(User).where(User.id == user_id))
    finally:
        await engine.dispose()
