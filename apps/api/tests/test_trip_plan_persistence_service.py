from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from app.db.models import TravelPlan, TravelPlanVersion
from app.schemas.trip_capabilities import CapabilityPlan, TripPlanningRequest
from app.schemas.trip_evidence import EvidenceStatus
from app.schemas.trip_itinerary import TripNarrativePlan
from app.schemas.trip_plan_snapshot import (
    TripHotelSnapshot,
    TripPlanSnapshot,
    TripPlanSourceMetadata,
    TripTransportSnapshot,
)
from app.schemas.trip_planning import CityTripRequest
from app.services.trip_plan_persistence_service import (
    TripPlanNotFoundError,
    TripPlanPersistenceService,
    TripPlanVersionArtifact,
)
from app.services.trip_presentation_context import build_trip_presentation_context

NOW = datetime(2026, 7, 30, tzinfo=UTC)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin(self) -> FakeTransaction:
        return FakeTransaction()

    async def flush(self) -> None:
        return None


class FakeSessionFactory:
    def __call__(self) -> FakeSession:
        return FakeSession()


class FakeTripPlanVersionRepository:
    def __init__(self) -> None:
        self.plan: TravelPlan | None = None
        self.versions: list[TravelPlanVersion] = []
        self.user_id = uuid4()

    async def lock_conversation(self, *_: object) -> object:
        return type("Conversation", (), {"user_id": self.user_id})()

    async def get_owned_plan(
        self,
        _: object,
        *,
        user_id: object,
        plan_id: object,
    ) -> TravelPlan | None:
        del user_id
        if self.plan is not None and self.plan.id == plan_id:
            return self.plan
        return None

    async def assistant_message_belongs_to_conversation(self, *_: object, **__: object) -> bool:
        return True

    async def get_version_by_assistant_message(
        self,
        _: object,
        assistant_message_id: object,
    ) -> TravelPlanVersion | None:
        return next(
            (item for item in self.versions if item.assistant_message_id == assistant_message_id),
            None,
        )

    async def get_plan_for_update(self, *_: object) -> TravelPlan | None:
        return self.plan

    async def get_version(
        self,
        _: object,
        *,
        plan_id: object,
        version: int,
    ) -> TravelPlanVersion | None:
        return next(
            (item for item in self.versions if item.plan_id == plan_id and item.version == version),
            None,
        )

    async def create_plan(
        self,
        _: object,
        *,
        user_id: object,
        conversation_id: object,
        title: str,
        request_json: dict[str, object],
        snapshot_json: dict[str, object],
    ) -> TravelPlan:
        self.plan = TravelPlan(
            id=uuid4(),
            user_id=user_id,
            conversation_id=conversation_id,
            title=title,
            status="active",
            current_version=0,
            request_json=request_json,
            plan_json=snapshot_json,
            created_at=NOW,
            updated_at=NOW,
        )
        return self.plan

    def add_version(self, _: object, version: TravelPlanVersion) -> None:
        version.id = uuid4()
        self.versions.append(version)


def snapshot() -> TripPlanSnapshot:
    return TripPlanSnapshot(
        request=TripPlanningRequest(
            core=CityTripRequest(
                destination_city="成都",
                duration_days=1,
                start_date=date(2026, 8, 1),
            )
        ),
        capabilities=CapabilityPlan(),
        days=[],
        transport=TripTransportSnapshot(
            enabled=False,
            status=EvidenceStatus.SKIPPED,
            query={"enabled": False},
            journey_scope="unspecified",
        ),
        hotel=TripHotelSnapshot(
            enabled=False,
            status=EvidenceStatus.SKIPPED,
            query={"enabled": False},
        ),
        overall_status="usable",
        source_metadata=TripPlanSourceMetadata(
            planning_run_id="run-1",
            generated_at=NOW,
        ),
    )


def artifact(
    *,
    user_id: UUID,
    conversation_id: UUID,
    assistant_message_id: UUID,
) -> TripPlanVersionArtifact:
    plan_snapshot = snapshot()
    return TripPlanVersionArtifact(
        user_id=user_id,
        conversation_id=conversation_id,
        assistant_message_id=assistant_message_id,
        snapshot=plan_snapshot,
        presentation_context=build_trip_presentation_context(plan_snapshot),
        narrative=TripNarrativePlan(
            title="成都一日旅行方案",
            summary="测试规划",
            days=[],
        ),
        rendered_markdown="# 成都一日旅行方案",
        user_instruction="把行程改轻松一点",
    )


@pytest.mark.asyncio
async def test_persistence_creates_immutable_parented_versions_and_is_idempotent() -> None:
    repository = FakeTripPlanVersionRepository()
    service = TripPlanPersistenceService(
        FakeSessionFactory(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
    )
    first_message_id = uuid4()
    conversation_id = uuid4()
    first_artifact = artifact(
        user_id=repository.user_id,
        conversation_id=conversation_id,
        assistant_message_id=first_message_id,
    )

    first = await service.save_completed_version(first_artifact)
    duplicate = await service.save_completed_version(first_artifact)
    second = await service.save_completed_version(
        artifact(
            user_id=repository.user_id,
            conversation_id=conversation_id,
            assistant_message_id=uuid4(),
        )
    )

    assert duplicate == first
    assert first.version == 1
    assert second.version == 2
    assert repository.plan is not None
    assert repository.plan.current_version == 2
    assert repository.plan.title == "成都1日旅行方案"
    assert len(repository.versions) == 2
    first_version, second_version = repository.versions
    assert first_version.parent_version_id is None
    assert first_version.invalidation_scope_json == {"scope": "initial_plan"}
    assert second_version.parent_version_id == first_version.id
    assert second_version.invalidation_scope_json == {"scope": "full_replan"}
    assert second_version.snapshot_json["schema_version"] == "trip_plan.v1"
    assert second_version.presentation_context_json["trip"]["destination_city"] == "成都"
    assert second_version.rendered_markdown == "# 成都一日旅行方案"

    loaded = await service.get_plan(
        uuid4(),
        first.plan_id,
        version=1,
    )
    assert loaded.version_id == first.version_id
    assert loaded.snapshot.request.core.destination_city == "成都"
    assert loaded.narrative is not None
    assert loaded.narrative.title == "成都一日旅行方案"

    with pytest.raises(TripPlanNotFoundError):
        await service.get_plan(uuid4(), uuid4())
