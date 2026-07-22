from __future__ import annotations

import os
import uuid

import pytest
from app.core.security import JwtCodec, hash_token
from app.core.settings import PROJECT_ROOT
from app.db.models import User, UserSession
from app.db.session import create_database
from app.services.auth_service import AuthService
from dotenv import load_dotenv
from sqlalchemy import delete, func, select

_JWT_SECRET = "database-jwt-test-secret-with-at-least-thirty-two-characters"


@pytest.mark.database
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_TESTS") != "1",
    reason="Set RUN_DATABASE_TESTS=1 to run PostgreSQL integration tests.",
)
async def test_phone_login_creates_one_user_and_multiple_revocable_sessions() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    engine, session_factory = create_database(os.environ["DATABASE_URL"])
    codec = JwtCodec(
        _JWT_SECRET,
        issuer="database-test",
        audience="database-test-web",
        access_token_minutes=15,
    )
    service = AuthService(session_factory, codec)
    phone = f"+86139{uuid.uuid4().int % 100_000_000:08d}"
    user_id = None
    try:
        first = await service.login_phone(
            phone,
            user_agent="pytest-first",
            ip_address="127.0.0.1",
        )
        user_id = first.user.id
        second = await service.login_phone(
            phone,
            user_agent="pytest-second",
            ip_address="127.0.0.2",
        )

        assert first.is_new_user is True
        assert second.is_new_user is False
        assert first.user.id == second.user.id
        first_claims = codec.decode_access_token(first.access_token)
        second_claims = codec.decode_access_token(second.access_token)
        assert first_claims.user_id == second_claims.user_id == first.user.id
        assert first_claims.session_id != second_claims.session_id

        async with session_factory() as session:
            user_count = await session.scalar(
                select(func.count()).select_from(User).where(User.phone_e164 == phone)
            )
            sessions = list(
                await session.scalars(
                    select(UserSession).where(UserSession.user_id == first.user.id)
                )
            )
        assert user_count == 1
        assert len(sessions) == 2
        assert {item.refresh_token_hash for item in sessions} == {
            hash_token(first.refresh_token),
            hash_token(second.refresh_token),
        }
        raw_tokens = {first.refresh_token, second.refresh_token}
        assert all(item.refresh_token_hash not in raw_tokens for item in sessions)
    finally:
        if user_id is not None:
            async with session_factory() as session, session.begin():
                await session.execute(delete(User).where(User.id == user_id))
        await engine.dispose()
