from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Message, TravelPlan, TravelPlanVersion


class TripPlanVersionRepository:
    async def get_owned_plan(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
    ) -> TravelPlan | None:
        return await session.scalar(
            select(TravelPlan).where(
                TravelPlan.id == plan_id,
                TravelPlan.user_id == user_id,
            )
        )

    async def list_owned_plans(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        limit: int = 100,
    ) -> list[TravelPlan]:
        result = await session.scalars(
            select(TravelPlan)
            .where(
                TravelPlan.user_id == user_id,
                TravelPlan.current_version > 0,
            )
            .order_by(TravelPlan.updated_at.desc())
            .limit(limit)
        )
        return list(result)

    async def lock_conversation(
        self,
        session: AsyncSession,
        conversation_id: uuid.UUID,
    ) -> Conversation | None:
        return await session.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update(of=Conversation)
        )

    async def assistant_message_belongs_to_conversation(
        self,
        session: AsyncSession,
        *,
        assistant_message_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:
        message_id = await session.scalar(
            select(Message.id).where(
                Message.id == assistant_message_id,
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
            )
        )
        return message_id is not None

    async def get_version_by_assistant_message(
        self,
        session: AsyncSession,
        assistant_message_id: uuid.UUID,
    ) -> TravelPlanVersion | None:
        return await session.scalar(
            select(TravelPlanVersion).where(
                TravelPlanVersion.assistant_message_id == assistant_message_id
            )
        )

    async def get_plan_for_update(
        self,
        session: AsyncSession,
        conversation_id: uuid.UUID,
    ) -> TravelPlan | None:
        return await session.scalar(
            select(TravelPlan)
            .where(TravelPlan.conversation_id == conversation_id)
            .with_for_update(of=TravelPlan)
        )

    async def get_owned_plan_for_update(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
    ) -> TravelPlan | None:
        return await session.scalar(
            select(TravelPlan)
            .where(
                TravelPlan.id == plan_id,
                TravelPlan.user_id == user_id,
            )
            .with_for_update(of=TravelPlan)
        )

    async def get_version(
        self,
        session: AsyncSession,
        *,
        plan_id: uuid.UUID,
        version: int,
    ) -> TravelPlanVersion | None:
        return await session.scalar(
            select(TravelPlanVersion).where(
                TravelPlanVersion.plan_id == plan_id,
                TravelPlanVersion.version == version,
            )
        )

    async def create_plan(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        title: str,
        request_json: dict[str, object],
        snapshot_json: dict[str, object],
    ) -> TravelPlan:
        plan = TravelPlan(
            user_id=user_id,
            conversation_id=conversation_id,
            title=title,
            status="active",
            current_version=0,
            request_json=request_json,
            plan_json=snapshot_json,
        )
        session.add(plan)
        await session.flush()
        return plan

    def add_version(
        self,
        session: AsyncSession,
        version: TravelPlanVersion,
    ) -> None:
        session.add(version)


__all__ = ["TripPlanVersionRepository"]
