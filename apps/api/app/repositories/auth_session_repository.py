from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

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

    async def get_for_access(
        self,
        session: AsyncSession,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        now: datetime,
    ) -> UserSession | None:
        return await session.scalar(
            select(UserSession)
            .options(joinedload(UserSession.user))
            .where(
                UserSession.id == session_id,
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
        )

    async def get_by_refresh_hash_for_update(
        self,
        session: AsyncSession,
        refresh_token_hash: str,
    ) -> UserSession | None:
        return await session.scalar(
            select(UserSession)
            .options(joinedload(UserSession.user, innerjoin=True))
            .where(UserSession.refresh_token_hash == refresh_token_hash)
            .with_for_update(of=UserSession)
        )


__all__ = ["AuthSessionRepository"]
