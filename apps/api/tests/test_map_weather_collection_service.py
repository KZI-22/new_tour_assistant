from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest
from app.graphs.trip_planner_nodes import MapWeatherNode
from app.schemas.map_planning import MapTripEvidence
from app.schemas.trip_capabilities import TripPlanningRequest
from app.schemas.trip_planning import (
    CityTripRequest,
    DailyWeatherEvidence,
    TripWeatherEvidence,
)
from app.services.map_trip_collection_service import MapTripCollectionError
from app.services.map_weather_collection_service import MapWeatherCollectionService


def _request() -> CityTripRequest:
    return CityTripRequest(
        destination_city="成都",
        start_date=date(2026, 8, 1),
        duration_days=2,
    )


def _map_evidence() -> MapTripEvidence:
    return MapTripEvidence(
        city="成都",
        planning_run_id="map-run",
        queried_at=datetime(2026, 7, 23, tzinfo=UTC),
        days=[],
    )


def _weather(*, available: bool = True) -> TripWeatherEvidence:
    return TripWeatherEvidence(
        city="成都",
        queried_at=datetime(2026, 7, 23, tzinfo=UTC),
        days=[
            DailyWeatherEvidence(
                date=date(2026, 8, day),
                coverage="available" if available else "unavailable",
                day_weather="晴" if available else None,
                unavailable_reason=None if available else "unavailable",
            )
            for day in (1, 2)
        ],
        warnings=[] if available else ["weather unavailable"],
    )


class FakeMapService:
    def __init__(
        self,
        *,
        result: MapTripEvidence | None = None,
        error: MapTripCollectionError | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.result = result or _map_evidence()
        self.error = error
        self.gate = gate
        self.started = asyncio.Event()
        self.cancelled = False

    async def collect(self, _: CityTripRequest) -> MapTripEvidence:
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return self.result


class FakeWeatherService:
    def __init__(
        self,
        result: TripWeatherEvidence,
        *,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.result = result
        self.gate = gate
        self.started = asyncio.Event()
        self.cancelled = False

    async def collect(self, **_: object) -> TripWeatherEvidence:
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self.result


@pytest.mark.asyncio
async def test_map_and_weather_collection_run_in_parallel() -> None:
    gate = asyncio.Event()
    map_service = FakeMapService(gate=gate)
    weather_service = FakeWeatherService(_weather(), gate=gate)
    service = MapWeatherCollectionService(
        map_service,  # type: ignore[arg-type]
        weather_service,  # type: ignore[arg-type]
        weather_timeout_seconds=1,
    )

    task = asyncio.create_task(service.collect(_request()))
    await asyncio.wait_for(
        asyncio.gather(map_service.started.wait(), weather_service.started.wait()),
        timeout=1,
    )
    gate.set()
    bundle = await task

    assert bundle.status == "usable"
    assert bundle.map is map_service.result
    assert bundle.weather is weather_service.result


@pytest.mark.asyncio
async def test_weather_unavailable_is_partial_and_map_remains_usable() -> None:
    service = MapWeatherCollectionService(
        FakeMapService(),  # type: ignore[arg-type]
        FakeWeatherService(_weather(available=False)),  # type: ignore[arg-type]
        weather_timeout_seconds=1,
    )

    bundle = await service.collect(_request())

    assert bundle.status == "partial"
    assert bundle.map is not None
    assert bundle.weather is not None
    assert bundle.warnings == ["weather unavailable"]


@pytest.mark.asyncio
async def test_map_collection_error_returns_failed_bundle() -> None:
    service = MapWeatherCollectionService(
        FakeMapService(error=MapTripCollectionError("MAP_ATTRACTIONS_EMPTY", "没有可靠景点")),  # type: ignore[arg-type]
        FakeWeatherService(_weather()),  # type: ignore[arg-type]
        weather_timeout_seconds=1,
    )

    bundle = await service.collect(_request())

    assert bundle.status == "failed"
    assert bundle.error_code == "MAP_ATTRACTIONS_EMPTY"
    assert bundle.warnings == ["没有可靠景点"]


@pytest.mark.asyncio
async def test_cancellation_reaches_map_and_weather_children() -> None:
    gate = asyncio.Event()
    map_service = FakeMapService(gate=gate)
    weather_service = FakeWeatherService(_weather(), gate=gate)
    service = MapWeatherCollectionService(
        map_service,  # type: ignore[arg-type]
        weather_service,  # type: ignore[arg-type]
        weather_timeout_seconds=1,
    )
    task = asyncio.create_task(service.collect(_request()))
    await asyncio.wait_for(
        asyncio.gather(map_service.started.wait(), weather_service.started.wait()),
        timeout=1,
    )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert map_service.cancelled is True
    assert weather_service.cancelled is True


@pytest.mark.asyncio
async def test_map_weather_graph_node_writes_only_its_state_key() -> None:
    service = MapWeatherCollectionService(
        FakeMapService(),  # type: ignore[arg-type]
        FakeWeatherService(_weather()),  # type: ignore[arg-type]
        weather_timeout_seconds=1,
    )
    node = MapWeatherNode(service)

    update = await node(
        {
            "request": TripPlanningRequest(core=_request()),
            "planning_run_id": "planning-run",
        }
    )

    assert set(update) == {"map_weather_evidence", "current_stage"}
    assert update["map_weather_evidence"].status == "usable"
