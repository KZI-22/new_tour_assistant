from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from app.clients.amap_client import AmapClient
from app.clients.amap_errors import AmapError
from app.core.request_context import get_request_context
from app.schemas.amap import (
    AmapToolData,
    AmapToolResult,
    CurrentCityInput,
    CurrentCityResult,
    RoutePlanInput,
    SearchPlacesInput,
    TravelTimeMatrixInput,
    WeatherInput,
)


def _success(data: AmapToolData) -> dict[str, Any]:
    return AmapToolResult(success=True, data=data).model_dump(mode="json")


def _failure(error: AmapError) -> dict[str, Any]:
    return AmapToolResult(
        success=False,
        error_code=error.error_code,
        error_message=str(error),
        provider_error_code=error.infocode,
    ).model_dump(mode="json")


def build_current_city_tool(client: AmapClient) -> StructuredTool:
    async def get_current_city(**values: Any) -> dict[str, Any]:
        CurrentCityInput.model_validate(values)
        context = get_request_context()
        if context is None:
            return _success(
                CurrentCityResult(
                    accuracy_level="unavailable",
                    locatable=False,
                    unavailable_reason="No FastAPI request context is available.",
                )
            )
        if not context.client_ip_is_public_ipv4 or not context.client_ip:
            return _success(
                CurrentCityResult(
                    accuracy_level="unavailable",
                    locatable=False,
                    unavailable_reason=(
                        "The request did not originate from a public IPv4 address that Amap "
                        "can locate."
                    ),
                )
            )
        try:
            return _success(await client.ip_location(context.client_ip))
        except AmapError as exc:
            return _failure(exc)

    return StructuredTool.from_function(
        coroutine=get_current_city,
        name="amap_get_current_city",
        description=(
            "Estimate the requester's city from the server-side public IPv4 request context. "
            "This is city-level IP inference, never precise GPS positioning, and the model "
            "cannot provide or inspect the IP address."
        ),
        args_schema=CurrentCityInput,
    )


def build_search_places_tool(client: AmapClient) -> StructuredTool:
    async def search_places(**values: Any) -> dict[str, Any]:
        query = SearchPlacesInput.model_validate(values)
        try:
            return _success(await client.search_places(query))
        except AmapError as exc:
            return _failure(exc)

    return StructuredTool.from_function(
        coroutine=search_places,
        name="amap_search_places",
        description=(
            "Search Amap POIs by keywords, optionally constrained by city/adcode or a GCJ-02 "
            "nearby-search center. Returns provider POI IDs, normalized addresses, categories, "
            "distances, and explicitly labeled GCJ-02 coordinates."
        ),
        args_schema=SearchPlacesInput,
    )


def build_plan_route_tool(client: AmapClient) -> StructuredTool:
    async def plan_route(**values: Any) -> dict[str, Any]:
        query = RoutePlanInput.model_validate(values)
        try:
            return _success(await client.plan_route(query))
        except AmapError as exc:
            return _failure(exc)

    return StructuredTool.from_function(
        coroutine=plan_route,
        name="amap_plan_route",
        description=(
            "Plan one Amap city route between two explicitly GCJ-02 coordinates. Supports "
            "walking, driving, transit, bicycling, and electric-bike modes; transit requires "
            "a city and waypoints are driving-only."
        ),
        args_schema=RoutePlanInput,
    )


def build_travel_time_matrix_tool(client: AmapClient) -> StructuredTool:
    async def travel_time_matrix(**values: Any) -> dict[str, Any]:
        query = TravelTimeMatrixInput.model_validate(values)
        try:
            return _success(await client.travel_time_matrix(query))
        except AmapError as exc:
            return _failure(exc)

    return StructuredTool.from_function(
        coroutine=travel_time_matrix,
        name="amap_travel_time_matrix",
        description=(
            "Calculate directed pairwise Amap distances and estimated durations for 2-20 "
            "GCJ-02 locations in driving or walking mode. Duplicate coordinates and self-pairs "
            "are removed, and provider batches are handled internally."
        ),
        args_schema=TravelTimeMatrixInput,
    )


def build_weather_tool(client: AmapClient) -> StructuredTool:
    async def get_weather(**values: Any) -> dict[str, Any]:
        query = WeatherInput.model_validate(values)
        try:
            return _success(await client.get_weather(query))
        except AmapError as exc:
            return _failure(exc)

    return StructuredTool.from_function(
        coroutine=get_weather,
        name="amap_get_weather",
        description=(
            "Retrieve current Amap weather and, optionally, the multi-day forecast for a city. "
            "A six-digit adcode is preferred; an unambiguous city name can be resolved internally."
        ),
        args_schema=WeatherInput,
    )


def build_amap_tools(client: AmapClient) -> list[StructuredTool]:
    return [
        build_current_city_tool(client),
        build_search_places_tool(client),
        build_plan_route_tool(client),
        build_travel_time_matrix_tool(client),
        build_weather_tool(client),
    ]


__all__ = [
    "build_amap_tools",
    "build_current_city_tool",
    "build_plan_route_tool",
    "build_search_places_tool",
    "build_travel_time_matrix_tool",
    "build_weather_tool",
]
