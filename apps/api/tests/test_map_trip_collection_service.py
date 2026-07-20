from __future__ import annotations

from datetime import date

import pytest
from app.schemas.amap import (
    AmapCoordinate,
    AmapPlace,
    MatrixEntry,
    PlaceSearchResult,
    RoutePlanInput,
    RouteResult,
    SearchPlacesInput,
    TravelTimeMatrixInput,
    TravelTimeMatrixResult,
)
from app.schemas.trip_planning import CityTripRequest
from app.services.map_trip_collection_service import MapTripCollectionService


def place(
    poi_id: str,
    name: str,
    offset: float,
    *,
    poi_type: str = "风景名胜",
    city: str = "成都市",
) -> AmapPlace:
    return AmapPlace(
        poi_id=poi_id,
        name=name,
        address=f"{name}地址",
        province="四川省",
        city=city,
        district="青羊区",
        adcode="510105",
        poi_type=poi_type,
        location=AmapCoordinate(longitude=104.0 + offset, latitude=30.0),
    )


class FakeMapClient:
    def __init__(
        self,
        *,
        attractions: list[AmapPlace],
        breakfasts: list[AmapPlace] | None = None,
        lunches: list[AmapPlace] | None = None,
        dinners: list[AmapPlace] | None = None,
        route_fails: bool = False,
    ) -> None:
        self.attractions = attractions
        self.breakfasts = breakfasts or []
        self.lunches = lunches or []
        self.dinners = dinners or []
        self.route_fails = route_fails
        self.search_queries: list[object] = []
        self.matrix_sizes: list[int] = []
        self.route_calls = 0

    async def search_places(self, query: SearchPlacesInput) -> PlaceSearchResult:
        self.search_queries.append(query)
        keyword = str(query.keywords)
        if keyword == "景点":
            return PlaceSearchResult(pois=self.attractions)
        if "早餐" in keyword:
            return PlaceSearchResult(pois=self.breakfasts)
        if "晚餐" in keyword:
            return PlaceSearchResult(pois=self.dinners)
        if "餐厅" in keyword:
            return PlaceSearchResult(pois=self.lunches)
        return PlaceSearchResult(pois=[])

    async def travel_time_matrix(
        self,
        query: TravelTimeMatrixInput,
    ) -> TravelTimeMatrixResult:
        locations = list(query.locations)
        self.matrix_sizes.append(len(locations))
        entries: list[MatrixEntry] = []
        for origin in locations:
            for destination in locations:
                if origin.id == destination.id:
                    continue
                distance = round(abs(origin.longitude - destination.longitude) * 1_000_000)
                entries.append(
                    MatrixEntry(
                        origin_id=origin.id,
                        destination_id=destination.id,
                        success=True,
                        distance_meters=distance,
                        duration_seconds=distance,
                    )
                )
        return TravelTimeMatrixResult(
            mode=query.mode,
            locations=locations,
            matrix=entries,
        )

    async def plan_route(self, _: RoutePlanInput) -> RouteResult:
        self.route_calls += 1
        if self.route_fails:
            raise RuntimeError("provider details must stay internal")
        return RouteResult(
            mode="transit",
            distance_meters=3_000,
            duration_seconds=900,
            route_summary="公交 1 次换乘",
            steps=[],
            transfers=1,
        )


@pytest.mark.asyncio
async def test_map_collection_optimizes_meals_and_preserves_fixed_role_order() -> None:
    client = FakeMapClient(
        attractions=[place("a1", "景点甲", 0.001), place("a2", "景点乙", 0.003)],
        breakfasts=[place("b-far", "远早餐", -0.010), place("b-near", "近早餐", 0.0005)],
        lunches=[place("l-far", "远午餐", 0.020), place("l-near", "近午餐", 0.002)],
        dinners=[place("d-far", "远晚餐", 0.010)],
    )

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
        )
    )

    day = evidence.days[0]
    assert [item.role for item in day.ordered_places()] == [
        "breakfast",
        "morning_attraction",
        "lunch",
        "afternoon_attraction",
        "dinner",
    ]
    assert day.breakfast and day.breakfast.poi_id == "b-near"
    assert day.lunch and day.lunch.poi_id == "l-near"
    assert day.afternoon_attraction and day.afternoon_attraction.poi_id == "a2"
    assert len({item.poi_id for item in day.ordered_places()}) == 5
    assert client.matrix_sizes == [2, 7]
    assert client.route_calls >= 1
    assert any(leg.mode == "transit" for leg in day.route_legs)


@pytest.mark.asyncio
async def test_map_collection_filters_invalid_attractions_and_warns_when_insufficient() -> None:
    client = FakeMapClient(
        attractions=[
            place("a1", "景点甲", 0.001),
            place("a1", "重复景点甲", 0.001),
            place("hotel", "测试酒店", 0.002, poi_type="住宿服务;宾馆酒店"),
            place("other-city", "外地景点", 0.003, city="重庆市"),
            place("a2", "景点乙", 0.004),
            place("a3", "景点丙", 0.008),
        ]
    )

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=2,
            start_date=date(2026, 7, 25),
        )
    )

    assert len(evidence.days) == 2
    assert evidence.days[0].afternoon_attraction is not None
    assert evidence.days[1].afternoon_attraction is None
    attraction_ids = {
        place.poi_id
        for day in evidence.days
        for place in (day.morning_attraction, day.afternoon_attraction)
        if place is not None
    }
    assert attraction_ids == {"a1", "a2", "a3"}
    assert any("只有一个有效景点" in warning for warning in evidence.warnings)


@pytest.mark.asyncio
async def test_transit_failure_keeps_known_walking_facts_and_marks_leg_unverified() -> None:
    client = FakeMapClient(
        attractions=[place("a1", "景点甲", 0.001), place("a2", "景点乙", 0.004)],
        route_fails=True,
    )

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
        )
    )

    leg = evidence.days[0].route_legs[0]
    assert leg.mode == "unverified"
    assert leg.distance_meters == 3_000
    assert leg.duration_seconds == 3_000
    assert "provider details" not in (leg.route_summary or "")
    assert any("出发前" in warning for warning in evidence.warnings)
