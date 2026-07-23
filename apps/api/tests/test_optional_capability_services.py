from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date

import pytest
from app.schemas.travel import (
    FlightSearchInput,
    FlyAIErrorCode,
    FlyAIResult,
    HotelSearchInput,
    TrainSearchInput,
)
from app.schemas.trip_capabilities import (
    HotelCapabilityPlan,
    JourneyScope,
    TransportCapabilityPlan,
    TransportMode,
)
from app.schemas.trip_evidence import EvidenceStatus
from app.services.hotel_search_service import HotelSearchService
from app.services.intercity_transport_service import IntercityTransportService


def _success(data: object) -> FlyAIResult:
    return FlyAIResult(
        success=True,
        command=["flyai", "redacted"],
        data=data,
        duration_ms=1,
    )


def _failure(code: FlyAIErrorCode) -> FlyAIResult:
    return FlyAIResult(
        success=False,
        command=["flyai", "redacted"],
        error_code=code,
        error_message="provider detail must not escape",
        duration_ms=1,
    )


class FakeFlyAIClient:
    def __init__(
        self,
        result: FlyAIResult | Callable[[str], FlyAIResult],
        *,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.result = result
        self.gate = gate
        self.flight_queries: list[FlightSearchInput] = []
        self.train_queries: list[TrainSearchInput] = []
        self.hotel_queries: list[HotelSearchInput] = []
        self.started = asyncio.Queue[str]()
        self.cancelled: set[str] = set()

    async def search_flight(self, query: FlightSearchInput) -> FlyAIResult:
        self.flight_queries.append(query)
        return await self._run("flight")

    async def search_train(self, query: TrainSearchInput) -> FlyAIResult:
        self.train_queries.append(query)
        return await self._run("train")

    async def search_hotel(self, query: HotelSearchInput) -> FlyAIResult:
        self.hotel_queries.append(query)
        return await self._run("hotel")

    async def _run(self, kind: str) -> FlyAIResult:
        await self.started.put(kind)
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled.add(kind)
            raise
        return self.result(kind) if callable(self.result) else self.result


def _transport_plan(
    *,
    enabled: bool = True,
    modes: list[TransportMode] | None = None,
    scope: JourneyScope = JourneyScope.ROUND_TRIP,
) -> TransportCapabilityPlan:
    return TransportCapabilityPlan(
        enabled=enabled,
        modes=modes or [TransportMode.FLIGHT],
        journey_scope=scope,
        origin="北京",
        destination="成都",
        outbound_date=date(2026, 8, 1),
        return_date=date(2026, 8, 3) if scope is JourneyScope.ROUND_TRIP else None,
        max_price=2_000,
    )


def _hotel_plan(*, enabled: bool = True) -> HotelCapabilityPlan:
    return HotelCapabilityPlan(
        enabled=enabled,
        destination="成都",
        check_in_date=date(2026, 8, 1),
        check_out_date=date(2026, 8, 3),
        nearby_poi="春熙路",
        keywords="亲子",
        hotel_stars=[4, 5],
        max_nightly_price=800,
    )


@pytest.mark.asyncio
async def test_disabled_capabilities_are_skipped_without_client_calls() -> None:
    client = FakeFlyAIClient(_success({"unused": True}))

    transport = await IntercityTransportService(client).search(_transport_plan(enabled=False))
    hotel = await HotelSearchService(client).search(_hotel_plan(enabled=False))

    assert transport.status is EvidenceStatus.SKIPPED
    assert hotel.status is EvidenceStatus.SKIPPED
    assert client.flight_queries == client.train_queries == client.hotel_queries == []


@pytest.mark.asyncio
async def test_round_trip_flight_and_train_queries_run_concurrently() -> None:
    gate = asyncio.Event()
    client = FakeFlyAIClient(
        lambda kind: _success({"provider": kind, "nested": [{"raw": [1, 2]}]}),
        gate=gate,
    )
    service = IntercityTransportService(client)
    task = asyncio.create_task(
        service.search(_transport_plan(modes=[TransportMode.FLIGHT, TransportMode.TRAIN]))
    )

    started = [await asyncio.wait_for(client.started.get(), timeout=1) for _ in range(4)]
    assert sorted(started) == ["flight", "flight", "train", "train"]
    gate.set()
    evidence = await task

    assert evidence.status is EvidenceStatus.USABLE
    assert len(client.flight_queries) == len(client.train_queries) == 2
    assert client.flight_queries[0].origin == "北京"
    assert client.flight_queries[1].origin == "成都"
    assert evidence.data == {
        "results": [
            {
                "mode": "flight",
                "direction": "outbound",
                "data": {"provider": "flight", "nested": [{"raw": [1, 2]}]},
            },
            {
                "mode": "flight",
                "direction": "return",
                "data": {"provider": "flight", "nested": [{"raw": [1, 2]}]},
            },
            {
                "mode": "train",
                "direction": "outbound",
                "data": {"provider": "train", "nested": [{"raw": [1, 2]}]},
            },
            {
                "mode": "train",
                "direction": "return",
                "data": {"provider": "train", "nested": [{"raw": [1, 2]}]},
            },
        ]
    }


@pytest.mark.asyncio
async def test_one_way_transport_does_not_query_return() -> None:
    client = FakeFlyAIClient(_success([{"opaque": True}]))

    evidence = await IntercityTransportService(client).search(
        _transport_plan(scope=JourneyScope.ONE_WAY)
    )

    assert evidence.status is EvidenceStatus.USABLE
    assert len(client.flight_queries) == 1
    assert evidence.query["queries"][0]["direction"] == "outbound"


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_data", [[], {}])
async def test_successful_empty_transport_data_is_empty(empty_data: object) -> None:
    client = FakeFlyAIClient(_success(empty_data))

    evidence = await IntercityTransportService(client).search(
        _transport_plan(scope=JourneyScope.ONE_WAY)
    )

    assert evidence.status is EvidenceStatus.EMPTY
    assert evidence.data is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        FlyAIErrorCode.CLI_TIMEOUT,
        FlyAIErrorCode.AUTH_ERROR,
        FlyAIErrorCode.CLI_NOT_FOUND,
    ],
)
async def test_transport_failures_use_safe_error_codes(code: FlyAIErrorCode) -> None:
    client = FakeFlyAIClient(_failure(code))

    evidence = await IntercityTransportService(client).search(
        _transport_plan(scope=JourneyScope.ONE_WAY)
    )

    assert evidence.status is EvidenceStatus.FAILED
    assert evidence.error_code == code.value
    assert "provider detail must not escape" not in " ".join(evidence.warnings)
    assert "flyai" not in str(evidence.query).casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        (_success({"arbitrary": [{"deep": {"json": True}}]}), EvidenceStatus.USABLE),
        (_success([]), EvidenceStatus.EMPTY),
        (_success({}), EvidenceStatus.EMPTY),
        (_failure(FlyAIErrorCode.CLI_TIMEOUT), EvidenceStatus.FAILED),
        (_failure(FlyAIErrorCode.AUTH_ERROR), EvidenceStatus.FAILED),
        (_failure(FlyAIErrorCode.CLI_NOT_FOUND), EvidenceStatus.FAILED),
    ],
)
async def test_hotel_results_remain_opaque(
    result: FlyAIResult,
    expected_status: EvidenceStatus,
) -> None:
    client = FakeFlyAIClient(result)

    evidence = await HotelSearchService(client).search(_hotel_plan())

    assert evidence.status is expected_status
    assert len(client.hotel_queries) == 1
    if expected_status is EvidenceStatus.USABLE:
        assert evidence.data is result.data
    else:
        assert evidence.data is None
    if result.error_message:
        assert result.error_message not in " ".join(evidence.warnings)


@pytest.mark.asyncio
async def test_transport_cancellation_reaches_all_started_queries() -> None:
    gate = asyncio.Event()
    client = FakeFlyAIClient(_success({"unused": True}), gate=gate)
    task = asyncio.create_task(
        IntercityTransportService(client).search(
            _transport_plan(modes=[TransportMode.FLIGHT, TransportMode.TRAIN])
        )
    )
    for _ in range(4):
        await asyncio.wait_for(client.started.get(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.cancelled == {"flight", "train"}
