from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import TravelPlan, TravelPlanVersion
from app.repositories.trip_plan_version_repository import TripPlanVersionRepository
from app.schemas.platform_planning import TravelPlanSummaryResponse
from app.schemas.travel_plan import TravelPlanDetailResponse
from app.schemas.trip_itinerary import TripNarrativePlan
from app.schemas.trip_plan_snapshot import (
    TripPlanSnapshot,
    TripPlanSnapshotAny,
    TripPlanSnapshotV2,
)

_SNAPSHOT_ADAPTER = TypeAdapter(TripPlanSnapshotAny)


class TripPlanPersistenceError(RuntimeError):
    pass


class TripPlanNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class TripPlanVersionArtifact:
    snapshot: TripPlanSnapshotAny
    narrative: TripNarrativePlan
    rendered_markdown: str
    user_instruction: str
    presentation_context: BaseModel | dict[str, Any] | None = None
    user_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    assistant_message_id: uuid.UUID | None = None
    edit_operations: list[dict[str, object]] = field(default_factory=list)
    invalidation_scope: dict[str, object] = field(default_factory=lambda: {"scope": "full_replan"})


@dataclass(frozen=True, slots=True)
class SavedTripPlanVersion:
    plan_id: uuid.UUID
    version_id: uuid.UUID
    version: int


class TripPlanVersionWriter(Protocol):
    async def save_completed_version(
        self,
        artifact: TripPlanVersionArtifact,
    ) -> SavedTripPlanVersion: ...


class TripPlanPersistenceService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository: TripPlanVersionRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or TripPlanVersionRepository()

    async def get_plan(
        self,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
        *,
        version: int | None = None,
    ) -> TravelPlanDetailResponse:
        async with self._session_factory() as session:
            plan = await self._repository.get_owned_plan(
                session,
                user_id=user_id,
                plan_id=plan_id,
            )
            if plan is None or plan.current_version < 1:
                raise TripPlanNotFoundError(str(plan_id))

            selected_version = version or plan.current_version
            plan_version = await self._repository.get_version(
                session,
                plan_id=plan.id,
                version=selected_version,
            )
            if plan_version is None or plan_version.validation_status != "valid":
                raise TripPlanNotFoundError(f"{plan_id}:v{selected_version}")

            return TravelPlanDetailResponse(
                plan_id=plan.id,
                version_id=plan_version.id,
                version=plan_version.version,
                title=plan.title,
                status=plan.status,
                current_version=plan.current_version,
                change_summary=plan_version.change_summary,
                created_at=plan_version.created_at,
                snapshot=_validate_snapshot(plan_version.snapshot_json),
                narrative=(
                    TripNarrativePlan.model_validate(plan_version.narrative_json)
                    if plan_version.narrative_json is not None
                    else None
                ),
                rendered_markdown=plan_version.rendered_markdown,
            )

    async def delete_plan(self, user_id: uuid.UUID, plan_id: uuid.UUID) -> None:
        async with self._session_factory() as session, session.begin():
            deleted = await self._repository.delete_owned_plan(
                session,
                user_id=user_id,
                plan_id=plan_id,
            )
            if not deleted:
                raise TripPlanNotFoundError(str(plan_id))

    async def list_plans(
        self,
        user_id: uuid.UUID,
    ) -> list[TravelPlanSummaryResponse]:
        async with self._session_factory() as session:
            plans = await self._repository.list_owned_plans(session, user_id=user_id)
            summaries: list[TravelPlanSummaryResponse] = []
            for plan in plans:
                summary = _plan_summary(plan)
                if summary is not None:
                    summaries.append(summary)
            return summaries

    async def save_completed_version(
        self,
        artifact: TripPlanVersionArtifact,
    ) -> SavedTripPlanVersion:
        now = datetime.now(UTC)
        snapshot_json = artifact.snapshot.model_dump(mode="json")
        request_json = artifact.snapshot.request.model_dump(mode="json")
        title = _plan_title(artifact.snapshot)

        async with self._session_factory() as session, session.begin():
            owner_id = artifact.user_id
            conversation = None
            if artifact.conversation_id is not None:
                conversation = await self._repository.lock_conversation(
                    session,
                    artifact.conversation_id,
                )
                if conversation is None:
                    raise TripPlanPersistenceError("conversation does not exist")
                owner_id = owner_id or conversation.user_id
                if owner_id != conversation.user_id:
                    raise TripPlanPersistenceError("conversation owner does not match plan owner")
            if owner_id is None:
                raise TripPlanPersistenceError("plan owner is required")

            if artifact.assistant_message_id is not None:
                if artifact.conversation_id is None:
                    raise TripPlanPersistenceError("assistant message requires a conversation")
                if not await self._repository.assistant_message_belongs_to_conversation(
                    session,
                    assistant_message_id=artifact.assistant_message_id,
                    conversation_id=artifact.conversation_id,
                ):
                    raise TripPlanPersistenceError(
                        "assistant message does not belong to the conversation"
                    )
                existing = await self._repository.get_version_by_assistant_message(
                    session,
                    artifact.assistant_message_id,
                )
                if existing is not None:
                    return SavedTripPlanVersion(
                        plan_id=existing.plan_id,
                        version_id=existing.id,
                        version=existing.version,
                    )

            if artifact.plan_id is not None:
                plan = await self._repository.get_owned_plan_for_update(
                    session,
                    user_id=owner_id,
                    plan_id=artifact.plan_id,
                )
                if plan is None:
                    raise TripPlanPersistenceError("travel plan does not exist")
            elif artifact.conversation_id is not None:
                plan = await self._repository.get_plan_for_update(
                    session,
                    artifact.conversation_id,
                )
            else:
                plan = None
            if plan is None:
                plan = await self._repository.create_plan(
                    session,
                    user_id=owner_id,
                    conversation_id=artifact.conversation_id,
                    title=title,
                    request_json=request_json,
                    snapshot_json=snapshot_json,
                )

            parent = (
                await self._repository.get_version(
                    session,
                    plan_id=plan.id,
                    version=plan.current_version,
                )
                if plan.current_version > 0
                else None
            )
            next_version = plan.current_version + 1
            invalidation_scope = (
                {"scope": "initial_plan"}
                if next_version == 1
                else dict(artifact.invalidation_scope)
            )
            version = TravelPlanVersion(
                plan_id=plan.id,
                parent_version_id=parent.id if parent is not None else None,
                assistant_message_id=artifact.assistant_message_id,
                version=next_version,
                schema_version=artifact.snapshot.schema_version,
                request_json=request_json,
                plan_json=snapshot_json,
                snapshot_json=snapshot_json,
                presentation_context_json=_serialize_context(artifact.presentation_context),
                narrative_json=artifact.narrative.model_dump(mode="json"),
                rendered_markdown=artifact.rendered_markdown,
                user_instruction=artifact.user_instruction,
                edit_operations_json=list(artifact.edit_operations),
                invalidation_scope_json=invalidation_scope,
                validation_status="valid",
                change_summary=(
                    "创建旅游规划"
                    if next_version == 1
                    else _change_summary(artifact.user_instruction)
                ),
                created_at=now,
            )
            self._repository.add_version(session, version)
            await session.flush()

            plan.title = title
            plan.status = "active"
            plan.current_version = next_version
            plan.request_json = request_json
            plan.plan_json = snapshot_json
            plan.updated_at = now

            return SavedTripPlanVersion(
                plan_id=plan.id,
                version_id=version.id,
                version=next_version,
            )


def _plan_title(snapshot: TripPlanSnapshot | TripPlanSnapshotV2) -> str:
    if isinstance(snapshot, TripPlanSnapshotV2):
        city = snapshot.request.destination_city
        duration = snapshot.request.duration_days
    else:
        city = snapshot.request.core.destination_city or "旅游"
        duration = snapshot.request.core.duration_days
    suffix = f"{duration}日旅行方案" if duration is not None else "旅行方案"
    return f"{city}{suffix}"[:200]


def _plan_summary(plan: TravelPlan) -> TravelPlanSummaryResponse | None:
    if plan.plan_json is None:
        return None
    try:
        snapshot = _validate_snapshot(plan.plan_json)
    except Exception:
        return None
    if isinstance(snapshot, TripPlanSnapshotV2):
        city = snapshot.request.destination_city
        start_date = snapshot.request.start_date
        duration_days = snapshot.request.duration_days
    else:
        core = snapshot.request.core
        if core.destination_city is None or core.start_date is None or core.duration_days is None:
            return None
        city = core.destination_city
        start_date = core.start_date
        duration_days = min(core.duration_days, 10)
    return TravelPlanSummaryResponse(
        plan_id=plan.id,
        title=plan.title,
        status=plan.status,
        current_version=plan.current_version,
        destination_city=city,
        start_date=start_date,
        duration_days=duration_days,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


def _serialize_context(
    context: BaseModel | dict[str, Any] | None,
) -> dict[str, Any] | None:
    if context is None:
        return None
    if isinstance(context, BaseModel):
        return context.model_dump(mode="json")
    return dict(context)


def _validate_snapshot(value: dict[str, Any]) -> TripPlanSnapshotAny:
    """Read snapshots written before the schema discriminator became mandatory."""
    if "schema_version" in value:
        return _SNAPSHOT_ADAPTER.validate_python(value)
    inferred_version = (
        "trip_plan.v1"
        if isinstance(value.get("request"), dict) and "core" in value["request"]
        else "trip_plan.v2"
    )
    return _SNAPSHOT_ADAPTER.validate_python({"schema_version": inferred_version, **value})


def _change_summary(user_instruction: str) -> str:
    normalized = " ".join(user_instruction.split())
    return f"根据用户指令更新：{normalized[:200]}" if normalized else "更新旅游规划"


__all__ = [
    "SavedTripPlanVersion",
    "TripPlanNotFoundError",
    "TripPlanPersistenceError",
    "TripPlanPersistenceService",
    "TripPlanVersionArtifact",
    "TripPlanVersionWriter",
]
