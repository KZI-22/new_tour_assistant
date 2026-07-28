from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

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
from app.schemas.trip_itinerary import TripDayNarrativeDraft, TripNarrativeDraft
from app.schemas.trip_planning import (
    CityTripRequest,
    DailyWeatherEvidence,
    TripWeatherEvidence,
)
from langchain_core.messages import AIMessageChunk

QUERY_TIME = datetime(2026, 7, 20, tzinfo=UTC)


class Runnable:
    def __init__(self, value: object) -> None:
        self._value = value

    async def ainvoke(self, _: object) -> object:
        return self._value


class FakeTripModel:
    def __init__(self, responses: dict[str, list[object]]) -> None:
        self._responses = defaultdict(list, responses)
        self.calls: list[str] = []

    def with_structured_output(self, schema: type[object]) -> Runnable:
        self.calls.append(f"native:{schema.__name__}")
        return Runnable(self._responses[schema.__name__].pop(0))

    async def astream(self, _: object):
        self.calls.append("stream:TripNarrativeDraft")
        value = self._responses["TripNarrativeDraft"].pop(0)
        assert isinstance(value, TripNarrativeDraft)
        payload = value.model_dump_json()
        midpoint = len(payload) // 2
        yield AIMessageChunk(content=payload[:midpoint])
        yield AIMessageChunk(content=payload[midpoint:])


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


def narrative() -> TripNarrativeDraft:
    return TripNarrativeDraft(
        title="成都一日攻略",
        summary="按地图与用户偏好整理。",
        days=[
            TripDayNarrativeDraft(
                theme="城市漫游",
                recommendation_reasons=["按既定地图顺序游览。"],
            )
        ],
    )


def planner():
    collection = FakeMapCollection()
    weather = FakeWeatherService()
    flyai = FakeFlyAIClient()
    return (
        StandardTripPlanner(
            collection,  # type: ignore[arg-type]
            weather,  # type: ignore[arg-type]
            flyai,  # type: ignore[arg-type]
            settings(),
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
    model = FakeTripModel(
        {
            "TripNarrativeDraft": [narrative()],
        }
    )

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
    assert deltas[0].startswith("# ")
    assert all(delta.startswith("## ") for delta in deltas[1:])
    assert not any(call.startswith("native:") for call in model.calls)
    assert model.calls.count("stream:TripNarrativeDraft") == 1
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
async def test_standard_graph_executes_only_explicit_transport_and_hotel_capabilities() -> None:
    trip_planner, collection, weather, flyai = planner()
    model = FakeTripModel(
        {
            "TripNarrativeDraft": [narrative()],
        }
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
    assert "## 城际交通结果" in answer
    assert "航班 CA1234" in answer
    assert "参考价 ¥680.00" in answer
    assert "## 酒店结果" in answer
    assert "酒店 A" in answer
    assert "参考价 ¥399" in answer
    assert answer.count("数据来源：FlyAI；查询时间：") == 2
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
