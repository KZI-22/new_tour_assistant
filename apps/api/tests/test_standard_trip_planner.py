from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.settings import Settings
from app.graphs.standard_trip_planner import StandardTripPlanner
from app.schemas.amap import AmapCoordinate
from app.schemas.chat import ChatMessage
from app.schemas.map_planning import (
    MapDayEvidence,
    MapPlaceEvidence,
    MapTripEvidence,
)
from app.schemas.tool_execution import (
    MessageDeltaEvent,
    PlanningStageEvent,
    PlanningTraceEvent,
)
from app.schemas.travel import FlyAIResult
from app.schemas.trip_planning import (
    CityTripRequest,
    DailyWeatherEvidence,
    TripWeatherEvidence,
)
from app.services.tool_execution import ToolExecutionContext
from app.services.trip_plan_persistence_service import (
    SavedTripPlanVersion,
    TripPlanVersionArtifact,
)
from langchain_core.messages import AIMessageChunk

QUERY_TIME = datetime(2026, 7, 20, tzinfo=UTC)


class Runnable:
    def __init__(self, value: object) -> None:
        self._value = value

    async def ainvoke(self, _: object) -> object:
        return self._value


class FakeTripModel:
    def __init__(
        self,
        responses: dict[str, list[object]],
        *,
        markdown: str = (
            "# 成都一日攻略\n\n"
            "本次未查询机票、火车、酒店。\n\n"
            "## Day 1\n\n"
            "地点 a1按既定顺序游览。预报含晴天，户外活动请注意防晒。"
        ),
    ) -> None:
        self._responses = defaultdict(list, responses)
        self._markdown = markdown
        self.calls: list[str] = []

    def with_structured_output(self, schema: type[object]) -> Runnable:
        self.calls.append(f"native:{schema.__name__}")
        return Runnable(self._responses[schema.__name__].pop(0))

    async def astream(self, _: object, **__: object):
        self.calls.append("stream:TripMarkdown")
        for start in range(0, len(self._markdown), 8):
            yield AIMessageChunk(content=self._markdown[start : start + 8])


class GatedTripModel(FakeTripModel):
    def __init__(self, first: str, rest: str) -> None:
        super().__init__({})
        self._first = first
        self._rest = rest
        self.released = asyncio.Event()

    async def astream(self, _: object, **__: object):
        self.calls.append("stream:TripMarkdown")
        yield AIMessageChunk(content=self._first)
        await self.released.wait()
        yield AIMessageChunk(content=self._rest)


class FailingTripModel(FakeTripModel):
    async def astream(self, _: object, **__: object):
        self.calls.append("stream:TripMarkdown")
        raise RuntimeError("provider failed")
        yield AIMessageChunk(content="")  # pragma: no cover


class RecordingVersionWriter:
    def __init__(self) -> None:
        self.artifacts: list[TripPlanVersionArtifact] = []

    async def save_completed_version(
        self,
        artifact: TripPlanVersionArtifact,
    ) -> SavedTripPlanVersion:
        self.artifacts.append(artifact)
        return SavedTripPlanVersion(
            plan_id=uuid4(),
            version_id=uuid4(),
            version=len(self.artifacts),
        )


class FakeMapCollection:
    def __init__(self) -> None:
        self.calls = 0

    async def collect(self, _: CityTripRequest) -> MapTripEvidence:
        self.calls += 1
        place = MapPlaceEvidence(
            reference_id="poi_a1",
            poi_id="a1",
            name="地点 a1",
            address="地址 a1",
            poi_type="风景名胜",
            location=AmapCoordinate(longitude=104.0, latitude=30.0),
            city="成都市",
            search_query="景点",
            search_rank=1,
            estimated_visit_minutes=90,
            candidate_score=42,
        )
        return MapTripEvidence(
            city="成都",
            planning_run_id="map-run",
            queried_at=QUERY_TIME,
            days=[
                MapDayEvidence(
                    day_index=1,
                    date=date(2027, 7, 25),
                    attractions=[place],
                    estimated_visit_minutes=90,
                )
            ],
        )


class FakeWeatherService:
    def __init__(self) -> None:
        self.calls = 0

    async def collect(self, **_: object) -> TripWeatherEvidence:
        self.calls += 1
        return TripWeatherEvidence(
            city="成都",
            queried_at=QUERY_TIME,
            days=[
                DailyWeatherEvidence(
                    date=date(2027, 7, 25),
                    coverage="available",
                    day_weather="晴",
                    night_weather="多云",
                    day_temperature="32",
                    night_temperature="23",
                )
            ],
        )


class FakeFlyAIClient:
    def __init__(self) -> None:
        self.flight_calls = 0
        self.train_calls = 0
        self.hotel_calls = 0

    async def search_flight(self, _: object) -> FlyAIResult:
        self.flight_calls += 1
        return FlyAIResult(
            success=True,
            command=["redacted"],
            data={
                "data": {
                    "itemList": [
                        {
                            "journeys": [
                                {
                                    "journeyType": "直达",
                                    "segments": [
                                        {
                                            "depDateTime": "2027-07-25 08:00:00",
                                            "depStationName": "南京禄口国际机场",
                                            "arrDateTime": "2027-07-25 10:30:00",
                                            "arrStationName": "成都天府机场",
                                            "marketingTransportName": "国航",
                                            "marketingTransportNo": "CA1234",
                                            "seatClassName": "经济舱",
                                        }
                                    ],
                                    "totalDuration": "150",
                                }
                            ],
                            "ticketPrice": "680.00",
                            "totalDuration": "150",
                        }
                    ]
                },
                "message": "success",
                "status": 0,
            },
            duration_ms=10,
        )

    async def search_train(self, _: object) -> FlyAIResult:
        self.train_calls += 1
        return FlyAIResult(
            success=True,
            command=["redacted"],
            data={"data": {"itemList": []}, "message": "success", "status": 0},
            duration_ms=10,
        )

    async def search_hotel(self, _: object) -> FlyAIResult:
        self.hotel_calls += 1
        return FlyAIResult(
            success=True,
            command=["redacted"],
            data={
                "data": {
                    "itemList": [
                        {
                            "name": "酒店 A",
                            "price": "¥399",
                            "star": "高档型",
                            "address": "锦江区测试路 1 号",
                        }
                    ]
                },
                "message": "success",
                "status": 0,
            },
            duration_ms=10,
        )


def settings() -> Settings:
    return Settings(
        app_name="test",
        model_config_path=Path("models.yaml"),
        cors_origins=(),
        log_level="WARNING",
    )


def planner(version_writer: RecordingVersionWriter | None = None):
    collection = FakeMapCollection()
    weather = FakeWeatherService()
    flyai = FakeFlyAIClient()
    return (
        StandardTripPlanner(
            collection,  # type: ignore[arg-type]
            weather,  # type: ignore[arg-type]
            flyai,  # type: ignore[arg-type]
            settings(),
            version_writer=version_writer,
        ),
        collection,
        weather,
        flyai,
    )


async def collect(
    trip_planner: StandardTripPlanner,
    model: FakeTripModel,
    message: str,
) -> list[object]:
    return [
        event
        async for event in trip_planner.stream(
            model,  # type: ignore[arg-type]
            [ChatMessage(role="user", content=message)],
            route_source="llm_router",
        )
    ]


@pytest.mark.asyncio
async def test_standard_graph_keeps_map_weather_fixed_and_skips_optional_queries() -> None:
    trip_planner, collection, weather, flyai = planner()
    model = FakeTripModel({})

    events = await collect(
        trip_planner,
        model,
        "帮我规划成都一日游，2027-07-25 开始",
    )

    deltas = [event.delta for event in events if isinstance(event, MessageDeltaEvent)]
    answer = "".join(deltas)
    stages = [
        (event.stage, event.status) for event in events if isinstance(event, PlanningStageEvent)
    ]
    traces = [event for event in events if isinstance(event, PlanningTraceEvent)]
    assert collection.calls == weather.calls == 1
    assert flyai.flight_calls == flyai.train_calls == flyai.hotel_calls == 0
    assert ("collecting_transport", "skipped") in stages
    assert ("collecting_hotels", "skipped") in stages
    assert "本次未查询机票、火车、酒店" in answer
    assert "27-32℃" not in answer
    assert "SPF50" not in answer
    assert "预报含晴天，户外活动请注意防晒。" in answer
    assert len(deltas) > 1
    assert answer.startswith("# 成都一日攻略")
    assert not any(call.startswith("native:") for call in model.calls)
    assert model.calls.count("stream:TripMarkdown") == 1
    validation_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, PlanningTraceEvent) and event.step == "validation_completed"
    )
    first_delta_index = next(
        index for index, event in enumerate(events) if isinstance(event, MessageDeltaEvent)
    )
    assert validation_index < first_delta_index
    capability_trace = next(trace for trace in traces if trace.title == "已解析本轮能力执行计划")
    assert capability_trace.data["transport_enabled"] is False
    assert capability_trace.data["hotel_enabled"] is False


@pytest.mark.asyncio
async def test_completed_standard_plan_is_persisted_as_a_version() -> None:
    writer = RecordingVersionWriter()
    trip_planner, _, _, _ = planner(writer)
    model = FakeTripModel({})
    context = ToolExecutionContext(
        conversation_id=uuid4(),
        assistant_message_id=uuid4(),
    )

    events = [
        event
        async for event in trip_planner.stream(
            model,  # type: ignore[arg-type]
            [
                ChatMessage(
                    role="user",
                    content="帮我规划成都一日游，2027-07-25 开始",
                )
            ],
            execution_context=context,
        )
    ]

    assert len(writer.artifacts) == 1
    artifact = writer.artifacts[0]
    assert artifact.conversation_id == context.conversation_id
    assert artifact.assistant_message_id == context.assistant_message_id
    assert artifact.snapshot.schema_version == "trip_plan.v1"
    assert artifact.presentation_context.trip.destination_city == "成都"
    assert artifact.user_instruction == "帮我规划成都一日游，2027-07-25 开始"
    assert artifact.rendered_markdown.startswith("# 成都一日攻略")
    saving_stages = [
        (event.status, event.detail)
        for event in events
        if isinstance(event, PlanningStageEvent) and event.stage == "saving_itinerary"
    ]
    assert saving_stages == [
        ("running", None),
        ("success", "版本 1"),
    ]


@pytest.mark.asyncio
async def test_standard_graph_streams_first_markdown_chunk_before_generation_finishes() -> None:
    trip_planner, _, _, _ = planner()
    model = GatedTripModel("# 成都一日攻略\n\n", "后续行程内容。")
    stream = trip_planner.stream(
        model,  # type: ignore[arg-type]
        [ChatMessage(role="user", content="帮我规划成都一日游，2027-07-25 开始")],
    )

    events_before_release: list[object] = []
    while True:
        event = await asyncio.wait_for(anext(stream), timeout=1)
        events_before_release.append(event)
        if isinstance(event, MessageDeltaEvent):
            break

    first_delta = events_before_release[-1]
    assert isinstance(first_delta, MessageDeltaEvent)
    assert first_delta.delta == "# 成都一日攻略\n\n"
    assert not model.released.is_set()
    assert not any(
        isinstance(event, PlanningTraceEvent) and event.step == "response_completed"
        for event in events_before_release
    )

    model.released.set()
    remaining = [event async for event in stream]

    assert (
        "".join(event.delta for event in remaining if isinstance(event, MessageDeltaEvent))
        == "后续行程内容。"
    )
    assert any(
        isinstance(event, PlanningTraceEvent) and event.step == "response_completed"
        for event in remaining
    )


@pytest.mark.asyncio
async def test_standard_graph_falls_back_to_validated_skeleton_before_first_chunk() -> None:
    trip_planner, _, _, _ = planner()
    model = FailingTripModel({})

    events = await collect(
        trip_planner,
        model,
        "帮我规划成都一日游，2027-07-25 开始",
    )

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    traces = [event for event in events if isinstance(event, PlanningTraceEvent)]
    assert answer.startswith("# 成都1日旅行方案")
    assert "地点 a1" in answer
    assert any(
        event.step == "response_completed"
        and event.status == "partial"
        and event.data["fallback"] == "deterministic_skeleton"
        for event in traces
    )


@pytest.mark.asyncio
async def test_standard_graph_executes_only_explicit_transport_and_hotel_capabilities() -> None:
    trip_planner, collection, weather, flyai = planner()
    model = FakeTripModel(
        {},
        markdown=(
            "# 成都一日攻略\n\n"
            "## 城际交通参考\n\n"
            "航班 CA1234｜南京禄口国际机场 08:00 → 成都天府机场 10:30"
            "｜约 150 分钟｜经济舱｜参考价 ¥680.00\n\n"
            "## 酒店参考\n\n"
            "酒店 A｜高档型｜参考价 ¥399｜锦江区测试路 1 号\n\n"
            "## Day 1\n\n地点 a1"
        ),
    )

    events = await collect(
        trip_planner,
        model,
        "帮我规划成都一日游，2027-07-25 开始，从南京出发，并查单程飞机、查酒店",
    )

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert collection.calls == weather.calls == 1
    assert flyai.flight_calls == 1
    assert flyai.train_calls == 0
    assert flyai.hotel_calls == 1
    assert "## 城际交通参考" in answer
    assert "航班 CA1234" in answer
    assert "参考价 ¥680.00" in answer
    assert "## 酒店参考" in answer
    assert "酒店 A" in answer
    assert "参考价 ¥399" in answer
    assert model.calls == ["stream:TripMarkdown"]
    assert "TripPlanningRequest" not in model.calls


@pytest.mark.asyncio
async def test_unified_missing_requirement_stops_before_all_provider_calls() -> None:
    trip_planner, collection, weather, flyai = planner()
    model = FakeTripModel({})

    events = await collect(
        trip_planner,
        model,
        "帮我规划成都一日游，2027-07-25 开始，并查单程飞机",
    )

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "交通出发城市" in answer
    assert collection.calls == weather.calls == 0
    assert flyai.flight_calls == flyai.train_calls == flyai.hotel_calls == 0
    assert not any(
        isinstance(event, PlanningStageEvent) and event.stage == "collecting_pois"
        for event in events
    )
    assert model.calls == []
