from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.graphs.trip_planner import _deterministic_itinerary, _supplement_request
from app.schemas.amap import SearchPlacesInput
from app.schemas.itinerary import TripRequest
from app.schemas.tool_execution import ToolResult, ToolResultMetadata
from app.schemas.travel import HotelSearchInput
from app.services.tool_execution import ToolExecutionContext, ToolExecutor
from app.services.travel_data_collector import (
    TravelDataCollector,
    _relevant_pois,
    _transport_options,
)
from langchain_core.tools import StructuredTool
from pydantic import ValidationError

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "flyai_transport_contract.json"


def _contract_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _transport_outcome(tool_name: str, data: dict[str, Any]) -> SimpleNamespace:
    result = ToolResult(
        success=True,
        tool_name=tool_name,
        data=data,
        metadata=ToolResultMetadata(
            provider="flyai",
            duration_ms=100,
            queried_at=datetime(2026, 7, 18, 2, 39, tzinfo=UTC),
        ),
    )
    return SimpleNamespace(result=result)


def test_explicit_duration_repairs_inconsistent_model_dates() -> None:
    extracted = TripRequest(
        origin="南京",
        destinations=["西安"],
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 22),
    )

    request = _supplement_request(
        extracted,
        "从南京出发，后天出发，玩2天",
        current_date=date(2026, 7, 18),
    )

    assert request.start_date == date(2026, 7, 20)
    assert request.end_date == date(2026, 7, 21)
    assert request.duration_days == 2


def test_incident_request_generates_exactly_two_calendar_days() -> None:
    request = _supplement_request(
        TripRequest(
            origin="南京",
            destinations=["西安"],
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 22),
        ),
        "从南京出发，后天出发，玩2天",
        current_date=date(2026, 7, 18),
    )

    plan = _deterministic_itinerary(
        {
            "transport_results": [],
            "hotel_results": [],
            "poi_results": [],
            "weather_results": [],
            "route_results": [],
            "tool_failures": [],
            "collection_diagnostics": {},
        },  # type: ignore[arg-type]
        request,
        max_daily_activities=5,
    )

    assert plan.title == "西安2日行程"
    assert [day.date for day in plan.days] == [date(2026, 7, 20), date(2026, 7, 21)]


def test_trip_request_rejects_conflicting_date_triplet() -> None:
    with pytest.raises(ValidationError, match="duration_days"):
        TripRequest(
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 22),
            duration_days=2,
        )


def test_real_flyai_flight_shape_normalizes_direct_and_transfer_options() -> None:
    fixture = _contract_fixture()["flight"]
    call = {
        "name": "search_flight",
        "args": {
            "origin": "南京",
            "destination": "西安",
            "departure_date": "2026-07-20",
        },
    }

    options = _transport_options(
        call,
        _transport_outcome("search_flight", fixture),
        TripRequest(),
        timezone="Asia/Shanghai",
    )

    assert len(options) == 2
    assert options[0].flight_number == "MU2795"
    assert options[0].price == 430
    assert options[0].duration_minutes == 120
    assert options[0].origin_station == "禄口国际机场"
    assert options[0].destination_station == "咸阳机场"
    assert options[1].flight_number == "MU1111 → MU2222"
    assert options[1].departure_city == "南京"
    assert options[1].arrival_city == "西安"
    assert options[1].duration_minutes == 285


def test_real_flyai_train_shape_inherits_item_price_and_minute_duration() -> None:
    fixture = _contract_fixture()["train"]
    call = {
        "name": "search_train",
        "args": {
            "origin": "南京",
            "destination": "西安",
            "departure_date": "2026-07-20",
        },
    }

    options = _transport_options(
        call,
        _transport_outcome("search_train", fixture),
        TripRequest(),
        timezone="Asia/Shanghai",
    )

    assert len(options) == 1
    assert options[0].train_number == "G94"
    assert options[0].price == 623
    assert options[0].duration_minutes == 278
    assert options[0].departure_time == datetime.fromisoformat("2026-07-20T09:56:00+08:00")


def test_poi_relevance_rejects_commercial_and_cross_city_results() -> None:
    request = TripRequest(
        destinations=["西安"],
        interests=["历史文化"],
    )
    candidates = [
        {
            "poi_id": "bad-commercial",
            "name": "山西地标产品展销中心",
            "city": "西安市",
            "poi_type": "购物服务",
            "query": "城市地标",
            "location": {"longitude": 108.9, "latitude": 34.2},
        },
        {
            "poi_id": "good",
            "name": "西安城墙",
            "city": "西安市",
            "poi_type": "风景名胜;历史遗址",
            "query": "历史文化景点",
            "location": {"longitude": 108.95, "latitude": 34.26},
        },
        {
            "poi_id": "wrong-city",
            "name": "平遥古城",
            "city": "晋中市",
            "poi_type": "风景名胜;历史遗址",
            "query": "历史文化景点",
            "location": {"longitude": 112.18, "latitude": 37.2},
        },
    ]

    result = _relevant_pois(candidates, request)

    assert [item["poi_id"] for item in result] == ["good"]
    assert result[0]["relevance_score"] >= 20
    assert "query_type_match" in result[0]["relevance_reasons"]


@pytest.mark.asyncio
async def test_hotel_geocoding_is_city_checked_and_bounded() -> None:
    geocode_calls = 0

    async def hotel(**_: Any) -> dict[str, Any]:
        return {
            "success": True,
            "provider": "fake-hotel",
            "data": {
                "items": [
                    {
                        "id": "h1",
                        "name": "西安城墙假日酒店",
                        "address": "西安市碑林区南大街88号",
                        "price": 480,
                    },
                    {
                        "id": "h2",
                        "name": "西安钟楼酒店",
                        "address": "西安市新城区东大街1号",
                        "price": 420,
                    },
                ]
            },
        }

    async def places(**_: Any) -> dict[str, Any]:
        nonlocal geocode_calls
        geocode_calls += 1
        return {
            "success": True,
            "provider": "amap",
            "data": {
                "pois": [
                    {
                        "id": "amap-h1",
                        "name": "西安城墙假日酒店",
                        "cityname": "西安市",
                        "adname": "碑林区",
                        "address": "南大街88号",
                        "type": "住宿服务;宾馆酒店",
                        "location": "108.946,34.258",
                    }
                ]
            },
        }

    tools = [
        StructuredTool.from_function(
            coroutine=hotel,
            name="search_hotel",
            description="hotel",
            args_schema=HotelSearchInput,
        ),
        StructuredTool.from_function(
            coroutine=places,
            name="amap_search_places",
            description="places",
            args_schema=SearchPlacesInput,
        ),
    ]
    collector = TravelDataCollector(
        ToolExecutor(tools),
        max_poi_candidates=10,
        max_hotel_geocodes=1,
        result_max_length=10_000,
    )

    result = await collector.collect(
        TripRequest(
            origin="南京",
            destinations=["西安"],
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 21),
        ),
        ["hotel"],
        execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        writer=lambda _: None,
    )

    assert geocode_calls == 1
    assert result["hotel_results"][0].coordinate_source == "amap_search_places"
    assert result["hotel_results"][0].coordinate_match_confidence == 0.98
    assert result["hotel_results"][0].coordinates is not None
    assert result["hotel_results"][1].coordinates is None
