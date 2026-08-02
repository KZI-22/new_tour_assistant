from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from app.clients.amap_errors import AmapRequestError
from app.core.request_context import use_request_context
from app.schemas.amap import (
    AmapCoordinate,
    AmapPlace,
    CurrentCityResult,
    CurrentWeather,
    MatrixLocation,
    PlaceSearchResult,
    RouteMode,
    RouteResult,
    TravelTimeMatrixResult,
    WeatherResult,
)
from app.schemas.context import CurrentTimeContext, TravelRequestContext
from app.tools.amap_tools import build_amap_tools
from pydantic import ValidationError


class FakeAmapClient:
    current_city_ip: str | None = None

    async def ip_location(self, client_ip: str) -> CurrentCityResult:
        self.current_city_ip = client_ip
        return CurrentCityResult(
            province="江苏省",
            city="南京市",
            adcode="320100",
            accuracy_level="city",
            locatable=True,
        )

    async def search_places(self, query: Any) -> PlaceSearchResult:
        is_restaurant = query.poi_type == "餐饮服务"
        return PlaceSearchResult(
            pois=[
                AmapPlace(
                    poi_id="B001",
                    name=query.keywords,
                    address="address",
                    province="江苏省",
                    city="南京市",
                    district="玄武区",
                    adcode="320102",
                    poi_type="餐饮服务;中餐厅" if is_restaurant else "博物馆",
                    location=AmapCoordinate(longitude=118.8, latitude=32.0),
                )
            ]
        )

    async def plan_route(self, query: Any) -> RouteResult:
        return RouteResult(
            mode=query.mode,
            distance_meters=1000,
            duration_seconds=600,
            route_summary="route",
            steps=[],
        )

    async def travel_time_matrix(self, query: Any) -> TravelTimeMatrixResult:
        return TravelTimeMatrixResult(mode=query.mode, locations=query.locations, matrix=[])

    async def get_weather(self, query: Any) -> WeatherResult:
        return WeatherResult(
            city="南京市",
            adcode=query.adcode,
            province="江苏省",
            current=CurrentWeather(
                weather="晴",
                temperature="30",
                humidity="50",
                wind_direction="东",
                wind_power="≤3",
                report_time="2026-07-13 10:00:00",
            ),
            forecast=[],
        )


def request_context() -> TravelRequestContext:
    return TravelRequestContext(
        client_ip="8.8.8.8",
        client_ip_is_public_ipv4=True,
        time=CurrentTimeContext(
            current_datetime=datetime.fromisoformat("2026-07-13T10:00:00+08:00"),
            current_date="2026-07-13",
            timezone="Asia/Shanghai",
            weekday="Monday",
        ),
    )


@pytest.mark.asyncio
async def test_tool_names_schemas_outputs_and_server_side_ip() -> None:
    client = FakeAmapClient()
    tools = build_amap_tools(client)  # type: ignore[arg-type]
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == {
        "amap_get_current_city",
        "amap_search_places",
        "amap_search_restaurants",
        "amap_plan_route",
        "amap_travel_time_matrix",
        "amap_get_weather",
    }
    current_schema = by_name["amap_get_current_city"].args_schema.model_json_schema()
    assert "ip" not in current_schema.get("properties", {})
    assert "city-level IP inference" in by_name["amap_get_current_city"].description

    with use_request_context(request_context()):
        current = await by_name["amap_get_current_city"].ainvoke({})
    places = await by_name["amap_search_places"].ainvoke(
        {"keywords": "南京博物院", "adcode": "320100"}
    )
    restaurants = await by_name["amap_search_restaurants"].ainvoke(
        {"city": "南京", "keyword": "本地特色美食"}
    )
    route = await by_name["amap_plan_route"].ainvoke(
        {
            "origin": {"longitude": 118.1, "latitude": 32.1},
            "destination": {"longitude": 118.2, "latitude": 32.2},
            "mode": "walking",
        }
    )
    matrix = await by_name["amap_travel_time_matrix"].ainvoke(
        {
            "locations": [
                MatrixLocation(id="a", name="A", longitude=118.1, latitude=32.1),
                MatrixLocation(id="b", name="B", longitude=118.2, latitude=32.2),
            ],
            "mode": "driving",
        }
    )
    weather = await by_name["amap_get_weather"].ainvoke({"adcode": "320100", "forecast": False})

    assert client.current_city_ip == "8.8.8.8"
    assert "8.8.8.8" not in str(current)
    assert current["data"]["accuracy_level"] == "city"
    assert places["data"]["pois"][0]["location"]["coordinate_system"] == "GCJ02"
    assert restaurants["data"]["pois"][0]["poi_type"].startswith("餐饮服务")
    assert route["data"]["mode"] == RouteMode.WALKING
    assert matrix["success"] is True
    assert weather["data"]["current"]["weather"] == "晴"


@pytest.mark.asyncio
async def test_private_ip_returns_structured_unavailable_result_without_calling_amap() -> None:
    client = FakeAmapClient()
    tool = build_amap_tools(client)[0]  # type: ignore[arg-type]
    context = request_context().model_copy(
        update={"client_ip": "127.0.0.1", "client_ip_is_public_ipv4": False}
    )

    with use_request_context(context):
        result = await tool.ainvoke({})

    assert result["success"] is True
    assert result["data"]["locatable"] is False
    assert client.current_city_ip is None


@pytest.mark.asyncio
async def test_amap_error_is_converted_to_a_structured_tool_error() -> None:
    class FailingClient(FakeAmapClient):
        async def search_places(self, query: Any) -> PlaceSearchResult:
            raise AmapRequestError(
                "The Amap service could not be reached.",
                infocode="10020",
            )

    tool = build_amap_tools(FailingClient())[1]  # type: ignore[arg-type]

    result = await tool.ainvoke({"keywords": "西湖"})

    assert result["success"] is False
    assert result["error_code"] == "REQUEST_ERROR"
    assert result["provider_error_code"] == "10020"
    assert result["data"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "origin": {"longitude": 181, "latitude": 32},
            "destination": {"longitude": 118, "latitude": 32},
            "mode": "walking",
        },
        {
            "origin": {
                "longitude": 118,
                "latitude": 32,
                "coordinate_system": "WGS84",
            },
            "destination": {"longitude": 119, "latitude": 32},
            "mode": "walking",
        },
        {
            "origin": {"longitude": 118, "latitude": 32},
            "destination": {"longitude": 119, "latitude": 32},
            "mode": "transit",
        },
        {
            "origin": {"longitude": 118, "latitude": 32},
            "destination": {"longitude": 119, "latitude": 32},
            "mode": "flying",
        },
    ],
)
def test_route_tool_schema_rejects_invalid_coordinates_system_and_mode(
    payload: dict[str, Any],
) -> None:
    tool = build_amap_tools(FakeAmapClient())[2]  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        tool.args_schema.model_validate(payload)
