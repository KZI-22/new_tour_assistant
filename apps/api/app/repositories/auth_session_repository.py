from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserSession


class AuthSessionRepository:
    async def create(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        refresh_token_hash: str,
        csrf_token_hash: str,
        expires_at: datetime,
        now: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> UserSession:
        auth_session = UserSession(
            user_id=user_id,
            refresh_token_hash=refresh_token_hash,
            csrf_token_hash=csrf_token_hash,
            expires_at=expires_at,
            last_used_at=now,
            created_at=now,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        session.add(auth_session)
        await session.flush()
        return auth_session


__all__ = ["AuthSessionRepository"]
