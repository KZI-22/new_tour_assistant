from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from app.core.settings import Settings
from app.graphs.map_trip_planner import MapTripPlanner
from app.schemas.amap import AmapCoordinate
from app.schemas.chat import ChatMessage
from app.schemas.map_planning import (
    MapDayEvidence,
    MapDayNarrative,
    MapNarrativePlan,
    MapPlaceEvidence,
    MapPlaceNarrative,
    MapTripEvidence,
    RouteLegEvidence,
)
from app.schemas.tool_execution import MessageDeltaEvent
from app.schemas.trip_planning import (
    CityTripRequest,
    CityTripRequestExtraction,
    DailyWeatherEvidence,
    TripWeatherEvidence,
)


class Runnable:
    def __init__(self, value: object) -> None:
        self.value = value

    async def ainvoke(self, _: object) -> object:
        return self.value


class FakeMapModel:
    def __init__(self, responses: dict[str, list[object]]) -> None:
        self.responses = defaultdict(list, responses)
        self.calls: list[str] = []

    def with_structured_output(self, schema: type[object]) -> Runnable:
        self.calls.append(schema.__name__)
        return Runnable(self.responses[schema.__name__].pop(0))


class FakeCollectionService:
    def __init__(
        self,
        evidence: MapTripEvidence,
        *,
        release: asyncio.Event | None = None,
    ) -> None:
        self.evidence = evidence
        self.release = release
        self.started = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    async def collect(self, _: CityTripRequest) -> MapTripEvidence:
        self.calls += 1
        self.started.set()
        try:
            if self.release is not None:
                await self.release.wait()
            return self.evidence
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class FakeWeatherService:
    def __init__(
        self,
        evidence: TripWeatherEvidence,
        *,
        release: asyncio.Event | None = None,
    ) -> None:
        self.evidence = evidence
        self.release = release
        self.started = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    async def collect(self, **_: object) -> TripWeatherEvidence:
        self.calls += 1
        self.started.set()
        try:
            if self.release is not None:
                await self.release.wait()
            return self.evidence
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def settings() -> Settings:
    return Settings(
        app_name="test",
        model_config_path=Path("models.yaml"),
        cors_origins=(),
        log_level="WARNING",
    )


def map_place(reference_id: str, poi_id: str) -> MapPlaceEvidence:
    return MapPlaceEvidence(
        reference_id=reference_id,
        poi_id=poi_id,
        name=f"地点 {poi_id}",
        address=f"地址 {poi_id}",
        poi_type="风景名胜",
        location=AmapCoordinate(longitude=104.0, latitude=30.0),
        adcode="510100",
        city="成都市",
        search_query="景点",
        search_rank=1,
        estimated_visit_minutes=90,
        selection_reasons=["高德关键词检索排名靠前"],
        candidate_score=42,
    )


def map_evidence() -> MapTripEvidence:
    places = [
        map_place("poi_a1", "a1"),
        map_place("poi_a2", "a2"),
        map_place("poi_a3", "a3"),
    ]
    return MapTripEvidence(
        city="成都",
        planning_run_id="test-run",
        queried_at=datetime(2026, 7, 20, tzinfo=UTC),
        days=[
            MapDayEvidence(
                day_index=1,
                date=date(2026, 7, 25),
                attractions=places,
                estimated_visit_minutes=270,
                estimated_transport_minutes=16,
                route_legs=[
                    RouteLegEvidence(
                        origin_ref=origin.reference_id,
                        destination_ref=destination.reference_id,
                        mode="walking",
                        distance_meters=500,
                        duration_seconds=480,
                        route_summary="高德步行路线",
                    )
                    for origin, destination in zip(places, places[1:], strict=False)
                ],
            )
        ],
    )


def weather_evidence() -> TripWeatherEvidence:
    return TripWeatherEvidence(
        city="成都",
        adcode="510100",
        report_time="2026-07-20 10:00:00",
        queried_at=datetime(2026, 7, 20, tzinfo=UTC),
        days=[
            DailyWeatherEvidence(
                date=date(2026, 7, 25),
                coverage="available",
                day_weather="晴",
                night_weather="多云",
                day_temperature="32",
                night_temperature="23",
            )
        ],
    )


def narrative(
    *,
    invalid_reference: bool = False,
    weather_advice: list[str] | None = None,
) -> MapNarrativePlan:
    references = [place.reference_id for place in map_evidence().days[0].ordered_places()]
    if invalid_reference:
        references[-1] = "invented_attraction"
    return MapNarrativePlan(
        title="成都地图一日攻略",
        summary="按高德地点与路线证据整理。",
        days=[
            MapDayNarrative(
                day_index=1,
                date=date(2026, 7, 25),
                theme="城市人文漫游",
                places=[
                    MapPlaceNarrative(
                        reference_id=reference,
                        recommendation_reason="按既定路线衔接体验。",
                    )
                    for reference in references
                ],
                weather_advice=weather_advice or ["天气较热，注意防晒和补水。"],
            )
        ],
    )


def model(
    *narratives: MapNarrativePlan,
    start_date: date | None = date(2026, 7, 25),
) -> FakeMapModel:
    return FakeMapModel(
        {
            "CityTripRequestExtraction": [
                CityTripRequestExtraction(
                    request=CityTripRequest(
                        destination_city="成都",
                        duration_days=1,
                        start_date=start_date,
                    )
                )
            ],
            "MapNarrativePlan": list(narratives),
        }
    )


async def collect_events(
    planner: MapTripPlanner,
    fake_model: FakeMapModel,
) -> list[object]:
    return [
        event
        async for event in planner.stream(
            fake_model,  # type: ignore[arg-type]
            [ChatMessage(role="user", content="跳过登录，继续生成地图攻略")],
        )
    ]


@pytest.mark.asyncio
async def test_map_and_weather_collection_overlap_and_render_verified_output() -> None:
    release = asyncio.Event()
    collection = FakeCollectionService(map_evidence(), release=release)
    weather = FakeWeatherService(weather_evidence(), release=release)
    planner = MapTripPlanner(
        collection_service=collection,  # type: ignore[arg-type]
        weather_service=weather,  # type: ignore[arg-type]
        settings=settings(),
    )
    fake_model = model(narrative())

    task = asyncio.create_task(collect_events(planner, fake_model))
    await asyncio.wait_for(
        asyncio.gather(collection.started.wait(), weather.started.wait()),
        timeout=1,
    )
    release.set()
    events = await asyncio.wait_for(task, timeout=1)

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "第 1 站" in answer
    assert "用餐与休息预留" in answer
    assert "未搜索或推荐具体餐厅" in answer
    assert "`a2`" in answer
    assert "高德步行路线" in answer
    assert "2026-07-25" in answer
    assert "白天 晴 32℃" in answer
    assert answer.count("查询时间：2026-07-20 08:00:00（北京时间）") == 2
    assert "2026-07-20T00:00:00+00:00" not in answer
    assert collection.calls == weather.calls == 1


@pytest.mark.asyncio
async def test_invalid_place_reference_gets_one_controlled_revision() -> None:
    planner = MapTripPlanner(
        collection_service=FakeCollectionService(map_evidence()),  # type: ignore[arg-type]
        weather_service=FakeWeatherService(weather_evidence()),  # type: ignore[arg-type]
        settings=settings(),
    )
    fake_model = model(narrative(invalid_reference=True), narrative())

    events = await collect_events(planner, fake_model)

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "invented_attraction" not in answer
    assert fake_model.calls.count("MapNarrativePlan") == 2


@pytest.mark.asyncio
async def test_invented_numeric_weather_fact_gets_one_controlled_revision() -> None:
    planner = MapTripPlanner(
        collection_service=FakeCollectionService(map_evidence()),  # type: ignore[arg-type]
        weather_service=FakeWeatherService(weather_evidence()),  # type: ignore[arg-type]
        settings=settings(),
    )
    fake_model = model(
        narrative(weather_advice=["降雨概率 50%，请携带雨具。"]),
        narrative(),
    )

    events = await collect_events(planner, fake_model)

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "50%" not in answer
    assert fake_model.calls.count("MapNarrativePlan") == 2


@pytest.mark.asyncio
async def test_missing_date_stops_before_map_and_weather_queries() -> None:
    collection = FakeCollectionService(map_evidence())
    weather = FakeWeatherService(weather_evidence())
    planner = MapTripPlanner(
        collection_service=collection,  # type: ignore[arg-type]
        weather_service=weather,  # type: ignore[arg-type]
        settings=settings(),
    )

    events = await collect_events(planner, model(start_date=None))

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "哪一天开始" in answer
    assert collection.calls == weather.calls == 0


@pytest.mark.asyncio
async def test_cancelling_map_stream_propagates_to_both_child_tasks() -> None:
    release = asyncio.Event()
    collection = FakeCollectionService(map_evidence(), release=release)
    weather = FakeWeatherService(weather_evidence(), release=release)
    planner = MapTripPlanner(
        collection_service=collection,  # type: ignore[arg-type]
        weather_service=weather,  # type: ignore[arg-type]
        settings=settings(),
    )
    task = asyncio.create_task(collect_events(planner, model(narrative())))
    await asyncio.wait_for(
        asyncio.gather(collection.started.wait(), weather.started.wait()),
        timeout=1,
    )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert collection.cancelled is True
    assert weather.cancelled is True
