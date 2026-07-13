from __future__ import annotations

import os

import pytest
from app.clients.amap_client import AmapClient
from app.core.settings import get_settings
from app.schemas.amap import (
    MatrixLocation,
    RouteMode,
    RoutePlanInput,
    SearchPlacesInput,
    TravelTimeMatrixInput,
    WeatherInput,
)

pytestmark = [
    pytest.mark.amap,
    pytest.mark.skipif(
        os.getenv("RUN_AMAP_TESTS") != "1",
        reason="set RUN_AMAP_TESTS=1 to consume real Amap API quota",
    ),
]


@pytest.mark.asyncio
async def test_real_amap_web_service_smoke() -> None:
    settings = get_settings()
    if not settings.amap_api_key:
        pytest.skip("AMAP_API_KEY is not configured")
    client = AmapClient(
        settings.amap_api_key,
        base_url=settings.amap_base_url,
        timeout_seconds=settings.amap_timeout_seconds,
        max_retries=settings.amap_max_retries,
    )
    origin = MatrixLocation(
        id="museum",
        name="南京博物院",
        longitude=118.815365,
        latitude=32.040384,
    )
    destination = MatrixLocation(id="gate", name="中山陵", longitude=118.848812, latitude=32.057537)
    try:
        places = await client.search_places(
            SearchPlacesInput(keywords="南京博物院", adcode="320100", limit=1)
        )
        weather = await client.get_weather(WeatherInput(adcode="320100", forecast=False))
        route = await client.plan_route(
            RoutePlanInput(
                origin={"longitude": origin.longitude, "latitude": origin.latitude},
                destination={
                    "longitude": destination.longitude,
                    "latitude": destination.latitude,
                },
                mode=RouteMode.DRIVING,
            )
        )
        matrix = await client.travel_time_matrix(
            TravelTimeMatrixInput(locations=[origin, destination])
        )
    finally:
        await client.aclose()

    assert places.pois
    assert weather.city
    assert route.distance_meters > 0
    assert len(matrix.matrix) == 2
