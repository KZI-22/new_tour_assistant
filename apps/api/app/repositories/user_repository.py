from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


class UserRepository:
    async def get_or_create_verified(
        self,
        session: AsyncSession,
        phone_e164: str,
        *,
        now: datetime,
    ) -> tuple[User, bool]:
        created_id = await session.scalar(
            insert(User)
            .values(
                phone_e164=phone_e164,
                status="active",
                phone_verified_at=now,
                last_login_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[User.phone_e164])
            .returning(User.id)
        )
        user = await session.scalar(
            select(User).where(User.phone_e164 == phone_e164).with_for_update()
        )
        if user is None:
            raise RuntimeError("User upsert did not return a persisted user.")
        return user, created_id is not None


__all__ = ["UserRepository"]
