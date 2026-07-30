from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from decimal import Decimal

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
from app.schemas.trip_options import HotelOptionSnapshot, TransportOptionSnapshot
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


def _transport_payload(kind: str = "train", *, item_count: int = 1) -> dict[str, object]:
    is_flight = kind == "flight"
    item: dict[str, object] = {
        "journeys": [
            {
                "journeyType": "直达",
                "segments": [
                    {
                        "depCityName": "北京",
                        "depDateTime": "2026-08-01 08:00:00",
                        "depStationName": "首都国际机场" if is_flight else "北京南站",
                        "depTerm": "T2" if is_flight else None,
                        "arrCityName": "成都",
                        "arrDateTime": "2026-08-01 10:30:00",
                        "arrStationName": "天府机场" if is_flight else "成都东站",
                        "arrTerm": "T2" if is_flight else None,
                        "marketingTransportName": "国航" if is_flight else "高铁",
                        "marketingTransportNo": "CA1234" if is_flight else "G123",
                        "seatClassName": "经济舱" if is_flight else "二等座",
                    }
                ],
                "totalDuration": "150",
            }
        ],
        "jumpUrl": "https://a.feizhu.com/example",
        "totalDuration": "150",
    }
    item["ticketPrice" if is_flight else "price"] = "680.00" if is_flight else "263.00"
    return {
        "data": {"itemList": [item for _ in range(item_count)]},
        "message": "success",
        "status": 0,
        "systemMessage": None,
    }


def _hotel_payload() -> dict[str, object]:
    return {
        "data": {
            "itemList": [
                {
                    "name": "酒店 A",
                    "star": "高档型",
                    "price": "¥399",
                    "interestsPoi": "近春熙路",
                    "address": "锦江区测试路 1 号",
                    "detailUrl": "https://a.feizhu.com/hotel-a",
                }
            ]
        },
        "message": "success",
        "status": 0,
        "systemMessage": None,
    }


def _empty_payload() -> dict[str, object]:
    return {"data": {"itemList": []}, "message": "success", "status": 0}


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
        lambda kind: _success(_transport_payload(kind, item_count=10)),
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
    assert len(evidence.data["results"]) == 4  # type: ignore[index]
    assert len(evidence.display_options) == 20
    assert any("去程航班 CA1234" in option for option in evidence.display_options)
    assert any("返程火车 G123" in option for option in evidence.display_options)


@pytest.mark.asyncio
async def test_one_way_transport_does_not_query_return() -> None:
    client = FakeFlyAIClient(_success(_transport_payload("flight")))

    evidence = await IntercityTransportService(client).search(
        _transport_plan(scope=JourneyScope.ONE_WAY)
    )

    assert evidence.status is EvidenceStatus.USABLE
    assert len(client.flight_queries) == 1
    assert evidence.query["queries"][0]["direction"] == "outbound"
    assert evidence.display_options == [
        "去程航班 CA1234（国航，直达）｜首都国际机场 T2 2026-08-01 08:00 → "
        "天府机场 T2 2026-08-01 10:30｜2小时30分｜经济舱｜参考价 ¥680.00"
        "｜[查看详情](https://a.feizhu.com/example)"
    ]
    assert len(evidence.normalized_options) == 1
    option = evidence.normalized_options[0]
    assert isinstance(option, TransportOptionSnapshot)
    assert option.transport_numbers == ["CA1234"]
    assert option.departure_station == "首都国际机场 T2"
    assert option.arrival_station == "天府机场 T2"
    assert option.duration_minutes == 150
    assert option.price_amount == Decimal("680.00")


@pytest.mark.asyncio
async def test_successful_empty_transport_data_is_empty() -> None:
    client = FakeFlyAIClient(_success(_empty_payload()))

    evidence = await IntercityTransportService(client).search(
        _transport_plan(scope=JourneyScope.ONE_WAY)
    )

    assert evidence.status is EvidenceStatus.EMPTY
    assert evidence.data is None


@pytest.mark.asyncio
async def test_nonempty_unrecognized_transport_data_is_failed() -> None:
    client = FakeFlyAIClient(_success({"arbitrary": [{"deep": {"json": True}}]}))

    evidence = await IntercityTransportService(client).search(
        _transport_plan(scope=JourneyScope.ONE_WAY)
    )

    assert evidence.status is EvidenceStatus.FAILED
    assert evidence.error_code == "PROVIDER_SCHEMA_INVALID"
    assert evidence.display_options == []


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
        (_success(_hotel_payload()), EvidenceStatus.USABLE),
        (_success(_empty_payload()), EvidenceStatus.EMPTY),
        (_success({"arbitrary": [{"deep": {"json": True}}]}), EvidenceStatus.FAILED),
        (_failure(FlyAIErrorCode.CLI_TIMEOUT), EvidenceStatus.FAILED),
        (_failure(FlyAIErrorCode.AUTH_ERROR), EvidenceStatus.FAILED),
        (_failure(FlyAIErrorCode.CLI_NOT_FOUND), EvidenceStatus.FAILED),
    ],
)
async def test_hotel_results_are_normalized_for_display(
    result: FlyAIResult,
    expected_status: EvidenceStatus,
) -> None:
    client = FakeFlyAIClient(result)

    evidence = await HotelSearchService(client).search(_hotel_plan())

    assert evidence.status is expected_status
    assert len(client.hotel_queries) == 1
    if expected_status is EvidenceStatus.USABLE:
        assert evidence.data is result.data
        assert evidence.display_options == [
            "酒店 A｜高档型｜参考价 ¥399｜近春熙路｜地址：锦江区测试路 1 号"
            "｜[查看详情](https://a.feizhu.com/hotel-a)"
        ]
        assert len(evidence.normalized_options) == 1
        option = evidence.normalized_options[0]
        assert isinstance(option, HotelOptionSnapshot)
        assert option.name == "酒店 A"
        assert option.price_amount == Decimal("399")
        assert option.address == "锦江区测试路 1 号"
    else:
        assert evidence.data is None
        assert evidence.normalized_options == []
        assert evidence.display_options == []
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
