from __future__ import annotations

from datetime import date

import pytest
from app.schemas.amap import (
    AmapCoordinate,
    AmapPlace,
    PlaceSearchResult,
    RoutePlanInput,
    RouteResult,
    SearchPlacesInput,
)
from app.schemas.trip_planning import CityTripRequest
from app.services.map_trip_collection_service import MapTripCollectionService


class RegressionClient:
    def __init__(self, places: list[AmapPlace]) -> None:
        self.places = places
        self.search_count = 0

    async def search_places(self, query: SearchPlacesInput) -> PlaceSearchResult:
        del query
        start = (self.search_count * 4) % max(1, len(self.places) - 1)
        self.search_count += 1
        attractions = self.places[:-1]
        rotated = attractions[start:] + attractions[:start]
        return PlaceSearchResult(pois=[*rotated[:9], self.places[-1]])

    async def plan_route(self, query: RoutePlanInput) -> RouteResult:
        return RouteResult(
            mode=query.mode,
            distance_meters=900 if query.mode == "walking" else 4_000,
            duration_seconds=720 if query.mode == "walking" else 1_500,
            route_summary=f"高德{query.mode}相邻路线",
            steps=[],
            transfers=0 if query.mode == "transit" else None,
            walking_distance_meters=300 if query.mode == "transit" else None,
        )


def city_places(city: str, days: int, poi_type: str) -> list[AmapPlace]:
    places: list[AmapPlace] = []
    for cluster in range(days):
        for index in range(4):
            places.append(
                AmapPlace(
                    poi_id=f"{city}-{cluster}-{index}",
                    name=f"{city}景点{cluster + 1}-{index + 1}",
                    address=f"{city}测试地址",
                    province="测试省",
                    city=f"{city}市",
                    district="测试区",
                    adcode="320100",
                    poi_type=poi_type if index != 3 else "风景名胜;特色街区",
                    location=AmapCoordinate(
                        longitude=118.0 + cluster * 0.25 + index * 0.002,
                        latitude=32.0,
                    ),
                )
            )
    places.append(
        AmapPlace(
            poi_id=f"{city}-parking",
            name=f"{city}景区停车场",
            address="测试地址",
            province="测试省",
            city=f"{city}市",
            district="测试区",
            adcode="320100",
            poi_type="交通设施;停车场",
            location=AmapCoordinate(longitude=118.0, latitude=32.0),
        )
    )
    return places


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("city", "days", "interests", "poi_type"),
    [
        ("南京", 3, ["历史文化"], "风景名胜;文物古迹"),
        ("杭州", 2, ["自然风光"], "风景名胜;公园广场"),
        ("上海", 1, ["城市地标"], "风景名胜;城市地标"),
        ("北京", 5, [], "风景名胜;文物古迹"),
        ("苏州", 3, ["园林人文"], "风景名胜;公园广场"),
    ],
)
async def test_fixed_city_regression_cases(
    city: str,
    days: int,
    interests: list[str],
    poi_type: str,
) -> None:
    client = RegressionClient(city_places(city, days, poi_type))

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city=city,
            duration_days=days,
            start_date=date(2026, 9, 1),
            interests=interests,
        )
    )

    attractions = [place for day in evidence.days for place in day.attractions]
    ids = [place.poi_id for place in attractions]
    assert days * 3 <= len(attractions) <= days * 4
    assert len(ids) == len(set(ids))
    assert not any("停车场" in place.name for place in attractions)
    assert all(3 <= len(day.attractions) <= 5 for day in evidence.days)
    assert all(
        day.estimated_visit_minutes + day.estimated_transport_minutes <= 480
        for day in evidence.days
    )
    assert all(
        [(leg.origin_ref, leg.destination_ref) for leg in day.route_legs]
        == list(
            zip(
                [place.reference_id for place in day.attractions],
                [place.reference_id for place in day.attractions][1:],
                strict=False,
            )
        )
        for day in evidence.days
    )
