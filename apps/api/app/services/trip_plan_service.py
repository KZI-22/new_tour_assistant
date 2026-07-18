from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import TravelPlan, TravelPlanVersion
from app.schemas.itinerary import ItineraryPlan, TripRequest


class TripPlanPersistenceError(RuntimeError):
    """Raised when a structured plan cannot be loaded or saved safely."""


@dataclass(frozen=True, slots=True)
class StoredTripPlan:
    id: uuid.UUID
    request: TripRequest
    plan: ItineraryPlan | None
    status: str
    version: int


class TripPlanService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_current(self, conversation_id: uuid.UUID) -> StoredTripPlan | None:
        async with self._session_factory() as session:
            record = await session.scalar(
                select(TravelPlan).where(TravelPlan.conversation_id == conversation_id)
            )
            if record is None:
                return None
            try:
                request = TripRequest.model_validate(record.request_json)
                plan = (
                    ItineraryPlan.model_validate(record.plan_json)
                    if record.plan_json is not None
                    else None
                )
            except Exception as exc:
                raise TripPlanPersistenceError("已保存的行程数据无法解析。") from exc
            return StoredTripPlan(
                id=record.id,
                request=request,
                plan=plan,
                status=record.status,
                version=record.current_version,
            )

    async def save_draft(
        self,
        conversation_id: uuid.UUID,
        request: TripRequest,
        *,
        title: str,
    ) -> uuid.UUID:
        now = datetime.now(UTC)
        request_json = request.model_dump(mode="json")
        try:
            async with self._session_factory() as session, session.begin():
                record = await session.scalar(
                    select(TravelPlan)
                    .where(TravelPlan.conversation_id == conversation_id)
                    .with_for_update()
                )
                if record is None:
                    record = TravelPlan(
                        conversation_id=conversation_id,
                        title=title,
                        status="draft",
                        current_version=0,
                        request_json=request_json,
                        plan_json=None,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(record)
                    await session.flush()
                elif record.current_version == 0:
                    record.title = title
                    record.status = "draft"
                    record.request_json = request_json
                    record.updated_at = now
                return record.id
        except Exception as exc:
            raise TripPlanPersistenceError("无法保存行程需求草稿。") from exc

    async def save_partial_plan(
        self,
        conversation_id: uuid.UUID,
        request: TripRequest,
        plan: ItineraryPlan,
    ) -> StoredTripPlan:
        """Save a recoverable structured draft without creating a formal version."""

        now = datetime.now(UTC)
        request_json = request.model_dump(mode="json")
        plan_json = plan.model_dump(mode="json")
        try:
            async with self._session_factory() as session, session.begin():
                record = await session.scalar(
                    select(TravelPlan)
                    .where(TravelPlan.conversation_id == conversation_id)
                    .with_for_update()
                )
                if record is None:
                    record = TravelPlan(
                        conversation_id=conversation_id,
                        title=plan.title,
                        status="draft",
                        current_version=0,
                        request_json=request_json,
                        plan_json=plan_json,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(record)
                    await session.flush()
                elif record.current_version == 0:
                    record.title = plan.title
                    record.status = "draft"
                    record.request_json = request_json
                    record.plan_json = plan_json
                    record.updated_at = now
                    await session.flush()
                else:
                    raise TripPlanPersistenceError("正式行程已存在，不能用未完成草稿覆盖。")
                record_id = record.id
        except TripPlanPersistenceError:
            raise
        except Exception as exc:
            raise TripPlanPersistenceError("无法保存结构化行程草稿。") from exc

        return StoredTripPlan(
            id=record_id,
            request=request,
            plan=plan,
            status="draft",
            version=0,
        )

    async def save_plan(
        self,
        conversation_id: uuid.UUID,
        request: TripRequest,
        plan: ItineraryPlan,
        *,
        change_summary: str | None = None,
    ) -> StoredTripPlan:
        now = datetime.now(UTC)
        request_json = request.model_dump(mode="json")
        plan_json = plan.model_dump(mode="json")
        try:
            async with self._session_factory() as session, session.begin():
                record = await session.scalar(
                    select(TravelPlan)
                    .where(TravelPlan.conversation_id == conversation_id)
                    .with_for_update()
                )
                if record is None:
                    record = TravelPlan(
                        conversation_id=conversation_id,
                        title=plan.title,
                        status="active",
                        current_version=0,
                        request_json=request_json,
                        plan_json=plan_json,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(record)
                    await session.flush()

                next_version = record.current_version + 1
                version = TravelPlanVersion(
                    plan_id=record.id,
                    version=next_version,
                    request_json=request_json,
                    plan_json=plan_json,
                    change_summary=change_summary,
                    created_at=now,
                )
                session.add(version)
                record.title = plan.title
                record.status = "active"
                record.current_version = next_version
                record.request_json = request_json
                record.plan_json = plan_json
                record.updated_at = now
                await session.flush()
                record_id = record.id
        except Exception as exc:
            raise TripPlanPersistenceError("无法保存行程方案版本。") from exc

        return StoredTripPlan(
            id=record_id,
            request=request,
            plan=plan,
            status="active",
            version=next_version,
        )


__all__ = [
    "StoredTripPlan",
    "TripPlanPersistenceError",
    "TripPlanService",
]
