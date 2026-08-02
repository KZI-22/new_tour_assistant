from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from app.schemas.travel import (
    FlightSearchInput,
    FlyAIExecutionDiagnostics,
    FlyAIResult,
    HotelSearchInput,
    TrainSearchInput,
)
from app.services.direct_travel_search_service import DirectTravelSearchService


class EmptyModelRegistry:
    def list_models(self) -> Any:
        return SimpleNamespace(default_model=None, models=[])


class FakeSearchClient:
    async def search_hotel(self, query: HotelSearchInput) -> FlyAIResult:
        return _result(
            [
                {
                    "name": "湖畔酒店",
                    "star": "五星",
                    "price": 680,
                    "address": "湖滨路 1 号",
                    "detailUrl": "https://example.com/hotel",
                }
            ]
        )

    async def search_flight(self, query: FlightSearchInput) -> FlyAIResult:
        return _result([_transport_item("MU5101", 920, "https://example.com/flight")])

    async def search_train(self, query: TrainSearchInput) -> FlyAIResult:
        return _result([_transport_item("G101", 320, "https://example.com/train", flight=False)])


def _result(items: list[dict[str, object]]) -> FlyAIResult:
    return FlyAIResult(
        success=True,
        command=["flyai", "search"],
        data={"status": 0, "data": {"itemList": items}},
        duration_ms=10,
        diagnostics=FlyAIExecutionDiagnostics(
            process_status="success",
            provider_status="success",
            parse_status="success",
            business_status="usable",
        ),
    )


def _transport_item(
    number: str,
    price: int,
    url: str,
    *,
    flight: bool = True,
) -> dict[str, object]:
    return {
        "journeys": [
            {
                "journeyType": "直达",
                "segments": [
                    {
                        "marketingTransportNo": number,
                        "marketingTransportName": "东方航空" if flight else "中国铁路",
                        "depStationName": "上海虹桥",
                        "arrStationName": "杭州东",
                        "depDateTime": "2026-09-01 08:00",
                        "arrDateTime": "2026-09-01 09:00",
                        "seatClassName": "经济舱" if flight else "二等座",
                    }
                ],
            }
        ],
        "totalDuration": 60,
        "ticketPrice" if flight else "price": price,
        "jumpUrl": url,
    }


@pytest.mark.asyncio
async def test_direct_search_keeps_provider_links_outside_llm_presentation() -> None:
    service = DirectTravelSearchService(FakeSearchClient(), EmptyModelRegistry())  # type: ignore[arg-type]

    hotel = await service.search_hotel(
        HotelSearchInput(
            destination="杭州",
            check_in_date=date(2026, 9, 1),
            check_out_date=date(2026, 9, 3),
        )
    )
    flight = await service.search_flight(
        FlightSearchInput(
            origin="上海",
            destination="杭州",
            departure_date=date(2026, 9, 1),
        )
    )
    train = await service.search_train(
        TrainSearchInput(
            origin="上海",
            destination="杭州",
            departure_date=date(2026, 9, 1),
        )
    )

    assert hotel.success and hotel.options[0].detail_url == "https://example.com/hotel"
    assert flight.success and flight.options[0].detail_url == "https://example.com/flight"
    assert train.success and train.options[0].detail_url == "https://example.com/train"
    assert hotel.summary == "为你整理了 1 个酒店结果，请结合时间和价格自行比较。"
