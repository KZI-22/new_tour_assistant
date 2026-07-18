from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date
from typing import Any

import pytest
from app.schemas.amap import RoutePlanInput, SearchPlacesInput, WeatherInput
from app.schemas.itinerary import Activity, AffectedSection, DayPlan, ItineraryPlan, TripRequest
from app.schemas.travel import HotelSearchInput, TrainSearchInput
from app.services.tool_execution import ToolExecutionContext, ToolExecutor
from app.services.travel_data_collector import TravelDataCollector
from langchain_core.tools import StructuredTool


@pytest.mark.asyncio
async def test_initial_queries_run_concurrently_without_precomputing_routes() -> None:
    active = 0
    max_active = 0

    async def initial_result(kind: str, values: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
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
    assert result["transport_results"]
    assert result["transport_results"][0].timezone == "Asia/Shanghai"
    assert result["transport_results"][0].departure_time.utcoffset().total_seconds() == 8 * 3600
    assert result["hotel_results"]
    assert len(result["poi_results"]) >= 2
    assert result["route_results"] == []
    transport_quality = [
        event
        for event in events
        if getattr(event, "tool_name", None) == "search_train"
        and getattr(event, "data_status", None) is not None
    ]
    assert transport_quality
    assert all(event.data_status == "usable" for event in transport_quality)
    assert all(event.normalized_item_count == 1 for event in transport_quality)


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
async def test_itinerary_routes_query_only_actual_adjacent_activities() -> None:
    route_calls: list[dict[str, Any]] = []

    async def route(**values: Any) -> dict[str, Any]:
        route_calls.append(values)
        return {
            "success": True,
            "provider": "fake",
            "data": {"distance_meters": 1000, "duration_seconds": 600},
        }

    route_tool = StructuredTool.from_function(
        coroutine=route,
        name="amap_plan_route",
        description="route",
        args_schema=RoutePlanInput,
    )
    collector = TravelDataCollector(
        ToolExecutor([route_tool]),
        max_poi_candidates=10,
        result_max_length=10_000,
    )
    request = TripRequest(
        origin="南京",
        destinations=["杭州"],
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 22),
    )
    plan = _route_test_plan(3)

    result = await collector.collect_itinerary_routes(
        plan,
        request,
        execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        writer=lambda _: None,
    )

    assert len(route_calls) == 2
    assert [call["origin"].longitude for call in route_calls] == [120.1, 120.11]
    assert result["diagnostic"]["required_items"] == 2
    assert result["diagnostic"]["usable_items"] == 2
    assert result["plan"].days[0].estimated_transport_time_minutes == 20


@pytest.mark.asyncio
async def test_transport_stage_fails_when_successful_calls_normalize_to_no_options() -> None:
    async def train(**_: Any) -> dict[str, Any]:
        return {
            "success": True,
            "provider": "fake",
            "data": {"items": [{"unexpected_number_field": "G1"}]},
        }

    tool = StructuredTool.from_function(
        coroutine=train,
        name="search_train",
        description="train",
        args_schema=TrainSearchInput,
    )
    collector = TravelDataCollector(
        ToolExecutor([tool]),
        max_poi_candidates=10,
        result_max_length=10_000,
    )
    events: list[Any] = []
    result = await collector.collect(
        TripRequest(
            origin="南京",
            destinations=["杭州"],
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 21),
            transport_preferences=["高铁"],
        ),
        ["transport"],
        execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        writer=events.append,
    )

    diagnostic = result["collection_diagnostics"]["collecting_transport"]
    assert result["transport_results"] == []
    assert diagnostic["successful_calls"] == 2
    assert diagnostic["usable_items"] == 0
    assert diagnostic["status"] == "failed"
    assert any(
        getattr(event, "stage", None) == "collecting_transport"
        and getattr(event, "status", None) == "failed"
        for event in events
    )
    quality_events = [
        event for event in events if getattr(event, "data_status", None) is not None
    ]
    assert len(quality_events) == 2
    assert all(event.success is False for event in quality_events)
    assert all(event.data_status == "invalid" for event in quality_events)
    assert all(event.error_code == "PROVIDER_SCHEMA_MISMATCH" for event in quality_events)
    assert all(event.provider_item_count == 1 for event in quality_events)
    assert all(event.normalized_item_count == 0 for event in quality_events)


@pytest.mark.asyncio
async def test_partial_route_results_preserve_only_verified_leg_duration() -> None:
    async def route(**values: Any) -> dict[str, Any]:
        if values["origin"].longitude > 120.1:
            return {
                "success": False,
                "provider": "fake",
                "error_code": "PROVIDER_RATE_LIMITED",
            }
        return {
            "success": True,
            "provider": "fake",
            "data": {"distance_meters": 1000, "duration_seconds": 600},
        }

    tools = [
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
    events: list[Any] = []
    result = await collector.collect_itinerary_routes(
        _route_test_plan(3),
        TripRequest(destinations=["杭州"]),
        execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        writer=events.append,
    )

    diagnostic = result["diagnostic"]
    assert diagnostic["status"] == "partial"
    assert diagnostic["usable_items"] == 1
    assert diagnostic["required_items"] == 2
    assert result["evidence"][0]["route_legs"][0]["duration_minutes"] == 10
    assert result["plan"].days[0].estimated_transport_time_minutes is None
    assert any(
        getattr(event, "stage", None) == "calculating_routes"
        and getattr(event, "status", None) == "partial"
        for event in events
    )


def _route_test_plan(activity_count: int) -> ItineraryPlan:
    return ItineraryPlan(
        title="杭州路线测试",
        destination="杭州",
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 20),
        days=[
            DayPlan(
                date=date(2026, 7, 20),
                day_index=1,
                activities=[
                    Activity(
                        place_name=f"地点 {index}",
                        poi_id=f"p{index}",
                        coordinates=json.dumps(
                            {
                                "longitude": 120.1 + index / 100,
                                "latitude": 30.2,
                                "coordinate_system": "GCJ02",
                            }
                        ),
                        activity_type="景点",
                    )
                    for index in range(activity_count)
                ],
            )
        ],
    )
