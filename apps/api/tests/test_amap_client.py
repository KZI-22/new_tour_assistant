from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import pytest
from app.clients.amap_client import AmapClient
from app.clients.amap_errors import (
    AmapConfigurationError,
    AmapError,
    AmapRateLimitError,
    AmapRequestError,
    AmapTimeoutError,
)
from app.schemas.amap import (
    AmapCoordinateInput,
    ConvertCoordinateInput,
    CoordinateSystem,
    MatrixLocation,
    RouteMode,
    RoutePlanInput,
    SearchPlacesInput,
    TravelTimeMatrixInput,
    WeatherInput,
)

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(
    handler: Handler,
    *,
    max_retries: int = 0,
    retry_delay_seconds: float = 0,
    min_request_interval_seconds: float = 0,
    matrix_batch_size: int = 100,
) -> AmapClient:
    http_client = httpx.AsyncClient(
        base_url="https://restapi.amap.test",
        transport=httpx.MockTransport(handler),
    )
    return AmapClient(
        "super-secret-amap-key",
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
        min_request_interval_seconds=min_request_interval_seconds,
        matrix_batch_size=matrix_batch_size,
        http_client=http_client,
    )


def ok(**values: object) -> dict[str, object]:
    return {"status": "1", "info": "OK", "infocode": "10000", **values}


def test_missing_api_key_is_a_configuration_error() -> None:
    with pytest.raises(AmapConfigurationError, match="AMAP_API_KEY"):
        AmapClient(None)


@pytest.mark.asyncio
async def test_search_places_supports_text_and_nearby_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=ok(
                pois=[
                    {
                        "id": "B001",
                        "name": "南京博物院",
                        "address": "中山东路321号",
                        "pname": "江苏省",
                        "cityname": "南京市",
                        "adname": "玄武区",
                        "adcode": "320102",
                        "type": "科教文化服务;博物馆",
                        "distance": "850",
                        "location": "118.815365,32.040384",
                    }
                ]
            ),
        )

    client = make_client(handler)
    text_result = await client.search_places(
        SearchPlacesInput(keywords="南京博物院", adcode="320100", limit=5)
    )
    nearby_result = await client.search_places(
        SearchPlacesInput(
            keywords="博物馆",
            location={
                "longitude": 118.796877,
                "latitude": 32.060255,
                "coordinate_system": "GCJ02",
            },
        )
    )

    assert text_result.pois[0].poi_id == "B001"
    assert text_result.pois[0].location.coordinate_system == "GCJ02"
    assert nearby_result.pois[0].distance_meters == 850
    assert [item.url.path for item in requests] == ["/v3/place/text", "/v3/place/around"]
    assert requests[0].url.params["city"] == "320100"
    assert requests[1].url.params["radius"] == "3000"


@pytest.mark.asyncio
async def test_search_places_returns_a_structured_empty_result() -> None:
    client = make_client(lambda _: httpx.Response(200, json=ok(pois=[])))

    result = await client.search_places(SearchPlacesInput(keywords="不存在的地点"))

    assert result.model_dump() == {"pois": []}


@pytest.mark.asyncio
async def test_ip_location_handles_success_and_unlocatable_city() -> None:
    responses = iter(
        [
            ok(province="江苏省", city="南京市", adcode="320100", rectangle=""),
            ok(province="局域网", city=[], adcode=[], rectangle=[]),
        ]
    )
    client = make_client(lambda _: httpx.Response(200, json=next(responses)))

    located = await client.ip_location("8.8.8.8")
    unavailable = await client.ip_location("192.168.1.2")

    assert located.locatable is True
    assert located.city == "南京市"
    assert located.accuracy_level == "city"
    assert unavailable.locatable is False
    assert unavailable.accuracy_level == "unavailable"


@pytest.mark.asyncio
async def test_weather_combines_current_conditions_and_forecast() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["extensions"] == "base":
            return httpx.Response(
                200,
                json=ok(
                    lives=[
                        {
                            "province": "江苏",
                            "city": "南京市",
                            "adcode": "320100",
                            "weather": "晴",
                            "temperature": "31",
                            "winddirection": "东",
                            "windpower": "≤3",
                            "humidity": "55",
                            "reporttime": "2026-07-13 10:00:00",
                        }
                    ]
                ),
            )
        return httpx.Response(
            200,
            json=ok(
                forecasts=[
                    {
                        "casts": [
                            {
                                "date": "2026-07-14",
                                "dayweather": "晴",
                                "nightweather": "多云",
                                "daytemp": "33",
                                "nighttemp": "25",
                                "daywind": "东",
                                "nightwind": "东",
                                "daypower": "≤3",
                                "nightpower": "≤3",
                            }
                        ]
                    }
                ]
            ),
        )

    result = await make_client(handler).get_weather(WeatherInput(adcode="320100", forecast=True))

    assert result.current.temperature == "31"
    assert result.forecast[0].date.isoformat() == "2026-07-14"
    assert result.adcode == "320100"


@pytest.mark.asyncio
async def test_geocode_reverse_geocode_and_coordinate_conversion_are_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/geocode/geo":
            return httpx.Response(
                200,
                json=ok(
                    geocodes=[
                        {
                            "formatted_address": "江苏省南京市玄武区南京博物院",
                            "province": "江苏省",
                            "city": "南京市",
                            "district": "玄武区",
                            "adcode": "320102",
                            "citycode": "025",
                            "location": "118.815365,32.040384",
                        }
                    ]
                ),
            )
        if request.url.path == "/v3/geocode/regeo":
            return httpx.Response(
                200,
                json=ok(
                    regeocode={
                        "formatted_address": "江苏省南京市玄武区中山东路321号",
                        "addressComponent": {
                            "province": "江苏省",
                            "city": "南京市",
                            "district": "玄武区",
                            "adcode": "320102",
                        },
                        "pois": [],
                    }
                ),
            )
        assert request.url.path == "/v3/assistant/coordinate/convert"
        return httpx.Response(200, json=ok(locations="118.815365,32.040384"))

    client = make_client(handler)
    geocodes = await client.geocode("南京博物院", city="南京")
    reversed_location = await client.reverse_geocode(
        AmapCoordinateInput(longitude=118.815365, latitude=32.040384)
    )
    converted = await client.convert_coordinates(
        ConvertCoordinateInput(
            longitude=118.81,
            latitude=32.04,
            source_coordinate_system=CoordinateSystem.WGS84,
        )
    )

    assert geocodes[0].citycode == "025"
    assert reversed_location.adcode == "320102"
    assert converted.coordinate_system == CoordinateSystem.GCJ02
    assert converted.source == "amap_conversion"


@pytest.mark.asyncio
async def test_driving_route_parses_v5_cost_and_steps() -> None:
    client = make_client(
        lambda request: httpx.Response(
            200,
            json=ok(
                route={
                    "paths": [
                        {
                            "distance": "3200",
                            "cost": {"duration": "1200"},
                            "steps": [
                                {
                                    "instruction": "沿中山东路向东行驶",
                                    "road_name": "中山东路",
                                    "step_distance": "1000",
                                    "cost": {"duration": "300"},
                                    "polyline": "118.1,32.1;118.2,32.2",
                                }
                            ],
                        }
                    ]
                }
            ),
            request=request,
        )
    )
    query = RoutePlanInput(
        origin={"longitude": 118.1, "latitude": 32.1},
        destination={"longitude": 118.2, "latitude": 32.2},
        mode=RouteMode.DRIVING,
    )

    result = await client.plan_route(query)

    assert result.distance_meters == 3200
    assert result.duration_seconds == 1200
    assert result.steps[0].road == "中山东路"


@pytest.mark.asyncio
async def test_matrix_deduplicates_self_pairs_and_splits_provider_batches() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        origin_count = len(request.url.params["origins"].split("|"))
        return httpx.Response(
            200,
            json=ok(
                results=[
                    {"distance": str(1000 + index), "duration": "600"}
                    for index in range(origin_count)
                ]
            ),
        )

    client = make_client(handler, matrix_batch_size=2)
    locations = [
        MatrixLocation(id="a", name="A", longitude=118.1, latitude=32.1),
        MatrixLocation(id="b", name="B", longitude=118.2, latitude=32.2),
        MatrixLocation(id="c", name="C", longitude=118.3, latitude=32.3),
        MatrixLocation(id="d", name="D", longitude=118.4, latitude=32.4),
        MatrixLocation(id="duplicate-a", name="A2", longitude=118.1, latitude=32.1),
    ]

    result = await client.travel_time_matrix(TravelTimeMatrixInput(locations=locations))

    assert [item.id for item in result.locations] == ["a", "b", "c", "d"]
    assert len(result.matrix) == 12
    assert all(item.origin_id != item.destination_id for item in result.matrix)
    assert len(calls) == 8
    assert all(len(item.url.params["origins"].split("|")) <= 2 for item in calls)


@pytest.mark.asyncio
async def test_business_failure_and_rate_limit_are_classified() -> None:
    client = make_client(
        lambda _: httpx.Response(
            200,
            json={"status": "0", "info": "INVALID_PARAMS", "infocode": "20000"},
        )
    )
    with pytest.raises(AmapError) as invalid:
        await client.search_places(SearchPlacesInput(keywords="西湖"))
    assert invalid.value.infocode == "20000"

    attempts = 0

    def rate_limited(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            json={"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "infocode": "10003"},
        )

    retrying_client = make_client(rate_limited, max_retries=1)
    with pytest.raises(AmapRateLimitError):
        await retrying_client.search_places(SearchPlacesInput(keywords="西湖"))
    assert attempts == 2


@pytest.mark.asyncio
async def test_rate_limit_retries_use_exponential_backoff() -> None:
    attempts = 0
    delays: list[float] = []

    def rate_limited(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            json={"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "infocode": "10003"},
        )

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    client = make_client(rate_limited, max_retries=2, retry_delay_seconds=0.25)
    client._sleep = record_delay

    with pytest.raises(AmapRateLimitError):
        await client.search_places(SearchPlacesInput(keywords="西湖"))

    assert attempts == 3
    assert delays == [0.25, 0.5]


@pytest.mark.asyncio
async def test_requests_are_throttled_across_concurrent_tool_calls() -> None:
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    client = make_client(
        lambda _: httpx.Response(200, json=ok(pois=[])),
        min_request_interval_seconds=0.2,
    )
    client._sleep = record_delay

    await client.search_places(SearchPlacesInput(keywords="西湖"))
    await client.search_places(SearchPlacesInput(keywords="灵隐寺"))

    assert len(delays) == 1
    assert 0 < delays[0] <= 0.201


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (httpx.ReadTimeout, AmapTimeoutError),
        (httpx.ConnectError, AmapRequestError),
    ],
)
async def test_network_failures_are_safe_and_classified(
    error_type: type[httpx.RequestError],
    expected: type[AmapError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("provider detail", request=request)

    with pytest.raises(expected):
        await make_client(handler).search_places(SearchPlacesInput(keywords="西湖"))


@pytest.mark.asyncio
async def test_api_key_never_appears_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client = make_client(
        lambda _: httpx.Response(
            200,
            json={
                "status": "0",
                "info": "invalid key super-secret-amap-key",
                "infocode": "10001",
            },
        )
    )

    with pytest.raises(AmapConfigurationError) as raised:
        await client.search_places(SearchPlacesInput(keywords="西湖"))

    assert "super-secret-amap-key" not in str(raised.value)
    assert "super-secret-amap-key" not in caplog.text
