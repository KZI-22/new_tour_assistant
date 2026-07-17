from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any

import pytest
from app.schemas.amap import RoutePlanInput, SearchPlacesInput, TravelTimeMatrixInput, WeatherInput
from app.schemas.itinerary import AffectedSection, TripRequest
from app.schemas.travel import HotelSearchInput, TrainSearchInput
from app.services.tool_execution import ToolExecutionContext, ToolExecutor
from app.services.travel_data_collector import TravelDataCollector
from langchain_core.tools import StructuredTool


@pytest.mark.asyncio
async def test_initial_queries_run_concurrently_before_dependent_routes() -> None:
    active = 0
    max_active = 0
    initial_finished = False
    completed_initial = 0
    route_started_after_initial: list[bool] = []

    async def initial_result(kind: str, values: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, completed_initial, initial_finished, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        completed_initial += 1
        initial_finished = completed_initial == 7
        if kind == "train":
            data: Any = {
                "items": [
                    {
                        "train_number": "G1",
                        "departure_time": "08:00",
                        "arrival_time": "10:00",
                        "price": 200,
                    }
                ]
            }
        elif kind == "hotel":
            data = {"items": [{"id": "h1", "name": "西湖酒店", "price": 500}]}
        elif kind == "poi":
            keyword = values["keywords"]
            offset = sum(ord(character) for character in keyword) % 20 / 100
            data = {
                "pois": [
                    {
                        "poi_id": f"{keyword}-id",
                        "name": keyword,
                        "address": "杭州",
                        "location": {
                            "longitude": 120.1 + offset,
                            "latitude": 30.2 + offset,
                            "coordinate_system": "GCJ02",
                        },
                    }
                ]
            }
        else:
            data = {"city": "杭州", "forecast": []}
        return {"success": True, "provider": "fake", "data": data}

    async def train(**values: Any) -> dict[str, Any]:
        return await initial_result("train", values)

    async def hotel(**values: Any) -> dict[str, Any]:
        return await initial_result("hotel", values)

    async def poi(**values: Any) -> dict[str, Any]:
        return await initial_result("poi", values)

    async def weather(**values: Any) -> dict[str, Any]:
        return await initial_result("weather", values)

    async def matrix(**values: Any) -> dict[str, Any]:
        del values
        route_started_after_initial.append(initial_finished)
        return {"success": True, "provider": "fake", "data": {"matrix": []}}

    async def route(**values: Any) -> dict[str, Any]:
        del values
        route_started_after_initial.append(initial_finished)
        return {
            "success": True,
            "provider": "fake",
            "data": {"distance_meters": 1000, "duration_seconds": 600},
        }

    tools = [
        StructuredTool.from_function(
            coroutine=train,
            name="search_train",
            description="train",
            args_schema=TrainSearchInput,
        ),
        StructuredTool.from_function(
            coroutine=hotel,
            name="search_hotel",
            description="hotel",
            args_schema=HotelSearchInput,
        ),
        StructuredTool.from_function(
            coroutine=poi,
            name="amap_search_places",
            description="poi",
            args_schema=SearchPlacesInput,
        ),
        StructuredTool.from_function(
            coroutine=weather,
            name="amap_get_weather",
            description="weather",
            args_schema=WeatherInput,
        ),
        StructuredTool.from_function(
            coroutine=matrix,
            name="amap_travel_time_matrix",
            description="matrix",
            args_schema=TravelTimeMatrixInput,
        ),
        StructuredTool.from_function(
            coroutine=route,
            name="amap_plan_route",
            description="route",
            args_schema=RoutePlanInput,
        ),
    ]
    collector = TravelDataCollector(
        ToolExecutor(tools),
        max_poi_candidates=10,
        result_max_length=10_000,
    )
    request = TripRequest(
        origin="南京",
        destinations=["杭州"],
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 22),
        transport_preferences=["高铁"],
        interests=["自然", "人文"],
    )
    events: list[Any] = []

    result = await collector.collect(
        request,
        [],
        execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        writer=events.append,
    )

    assert max_active >= 4
    assert route_started_after_initial and all(route_started_after_initial)
    assert result["transport_results"]
    assert result["transport_results"][0].timezone == "Asia/Shanghai"
    assert result["transport_results"][0].departure_time.utcoffset().total_seconds() == 8 * 3600
    assert result["hotel_results"]
    assert len(result["poi_results"]) >= 2
    assert len(result["route_results"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("affected_sections", "expected_calls"),
    [
        (["transport"], {"train"}),
        (["hotel"], {"hotel"}),
    ],
)
async def test_revision_queries_only_affected_tool_groups(
    affected_sections: list[AffectedSection],
    expected_calls: set[str],
) -> None:
    calls: list[str] = []

    async def train(**_: Any) -> dict[str, Any]:
        calls.append("train")
        return {"success": True, "provider": "fake", "data": {"items": []}}

    async def hotel(**_: Any) -> dict[str, Any]:
        calls.append("hotel")
        return {"success": True, "provider": "fake", "data": {"items": []}}

    tools = [
        StructuredTool.from_function(
            coroutine=train,
            name="search_train",
            description="train",
            args_schema=TrainSearchInput,
        ),
        StructuredTool.from_function(
            coroutine=hotel,
            name="search_hotel",
            description="hotel",
            args_schema=HotelSearchInput,
        ),
    ]
    collector = TravelDataCollector(
        ToolExecutor(tools),
        max_poi_candidates=10,
        result_max_length=10_000,
    )
    request = TripRequest(
        origin="南京",
        destinations=["杭州"],
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 22),
        transport_preferences=["高铁"],
    )

    await collector.collect(
        request,
        affected_sections,
        execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        writer=lambda _: None,
    )

    assert set(calls) == expected_calls


@pytest.mark.asyncio
async def test_activity_revision_recalculates_routes_from_existing_pois() -> None:
    matrix_calls = 0

    async def matrix(**_: Any) -> dict[str, Any]:
        nonlocal matrix_calls
        matrix_calls += 1
        return {"success": True, "provider": "fake", "data": {"matrix": []}}

    matrix_tool = StructuredTool.from_function(
        coroutine=matrix,
        name="amap_travel_time_matrix",
        description="matrix",
        args_schema=TravelTimeMatrixInput,
    )
    collector = TravelDataCollector(
        ToolExecutor([matrix_tool]),
        max_poi_candidates=10,
        result_max_length=10_000,
    )
    request = TripRequest(
        origin="南京",
        destinations=["杭州"],
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 22),
    )
    seed_pois = [
        {
            "poi_id": "p1",
            "name": "西湖",
            "location": {"longitude": 120.15, "latitude": 30.25},
        },
        {
            "poi_id": "p2",
            "name": "灵隐寺",
            "location": {"longitude": 120.10, "latitude": 30.24},
        },
    ]

    await collector.collect(
        request,
        ["activities"],
        execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        writer=lambda _: None,
        seed_pois=seed_pois,
    )

    assert matrix_calls == 1
