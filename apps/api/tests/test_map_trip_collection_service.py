from __future__ import annotations

import asyncio
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
        abnormal_transit: bool = False,
        route_fails: bool = False,
        failed_keywords: set[str] | None = None,
    ) -> None:
        self.attractions = attractions
        self.abnormal_transit = abnormal_transit
        self.route_fails = route_fails
        self.failed_keywords = failed_keywords or set()
        self.search_queries: list[SearchPlacesInput] = []
        self.route_queries: list[RoutePlanInput] = []
        self.active_searches = 0
        self.max_active_searches = 0

    async def search_places(self, query: SearchPlacesInput) -> PlaceSearchResult:
        self.search_queries.append(query)
        if str(query.keywords) in self.failed_keywords:
            raise RuntimeError("private provider failure")
        self.active_searches += 1
        self.max_active_searches = max(self.max_active_searches, self.active_searches)
        self.active_searches -= 1
        return PlaceSearchResult(pois=self.attractions)

    async def plan_route(self, query: RoutePlanInput) -> RouteResult:
        self.route_queries.append(query)
        if self.route_fails:
            raise RuntimeError("provider details must stay internal")
        if query.mode == "transit" and self.abnormal_transit:
            return RouteResult(
                mode=query.mode,
                distance_meters=8_000,
                duration_seconds=7_200,
                route_summary="公交换乘过多",
                steps=[],
                transfers=3,
                walking_distance_meters=2_500,
            )
        return RouteResult(
            mode=query.mode,
            distance_meters=800 if query.mode == "walking" else 3_000,
            duration_seconds=600,
            route_summary=f"高德{query.mode}路线",
            steps=[],
            transfers=0 if query.mode == "transit" else None,
            walking_distance_meters=300 if query.mode == "transit" else None,
        )


def standard_attractions() -> list[AmapPlace]:
    return [
        place("a1", "历史博物馆", 0.000, poi_type="科教文化服务;博物馆"),
        place("a2", "文化遗址", 0.005, poi_type="风景名胜;文物古迹"),
        place("a3", "人民公园", 0.010, poi_type="风景名胜;公园广场"),
        place("a4", "城市观景台", 0.015, poi_type="风景名胜;观景点"),
        place("a5", "老街", 0.020, poi_type="风景名胜;特色街区"),
    ]


@pytest.mark.asyncio
async def test_collection_uses_fixed_poi_recall_and_only_final_adjacent_routes() -> None:
    client = FakeMapClient(attractions=standard_attractions())

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
            interests=["历史文化"],
        )
    )

    day = evidence.days[0]
    assert 3 <= len(day.attractions) <= 4
    assert len(day.route_legs) == len(day.attractions) - 1
    assert [(leg.origin_ref, leg.destination_ref) for leg in day.route_legs] == list(
        zip(
            [item.reference_id for item in day.attractions],
            [item.reference_id for item in day.attractions][1:],
            strict=False,
        )
    )
    keywords = {str(query.keywords) for query in client.search_queries}
    assert {"风景名胜", "历史古迹", "博物馆", "公园", "城市地标", "特色街区"} <= keywords
    assert {"文化遗址", "名人故居", "古建筑"} <= keywords
    assert not any("餐" in keyword for keyword in keywords)
    assert len(client.route_queries) == len(day.route_legs)
    assert all(place.estimated_visit_minutes > 0 for place in day.attractions)


@pytest.mark.asyncio
async def test_collection_filters_facilities_city_mismatches_and_fuzzy_duplicates() -> None:
    client = FakeMapClient(
        attractions=[
            place("park-main", "成都人民公园景区", 0.001, poi_type="风景名胜;公园广场"),
            place("park-copy", "人民公园", 0.0011, poi_type="风景名胜;公园广场"),
            place("parking", "人民公园停车场", 0.0012, poi_type="交通设施;停车场"),
            place("other-city", "外地景点", 0.003, city="重庆市"),
            place("a2", "历史博物馆", 0.004, poi_type="科教文化服务;博物馆"),
            place("a3", "文化古街", 0.008, poi_type="风景名胜;特色街区"),
            place("a4", "城市广场", 0.012, poi_type="风景名胜;公园广场"),
        ]
    )

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=2,
            start_date=date(2026, 7, 25),
        )
    )

    ids = [item.poi_id for day in evidence.days for item in day.attractions]
    assert "parking" not in ids
    assert "other-city" not in ids
    assert len({"park-main", "park-copy"} & set(ids)) == 1
    assert len(ids) == len(set(ids))
    assert [len(day.attractions) for day in evidence.days] == [2, 2]
    assert any("少于每天 3 个" in warning for warning in evidence.warnings)


@pytest.mark.asyncio
async def test_abnormal_transit_falls_back_to_driving() -> None:
    attractions = [
        place("a1", "景点甲", 0.000),
        place("a2", "景点乙", 0.030),
        place("a3", "景点丙", 0.060),
    ]
    client = FakeMapClient(attractions=attractions, abnormal_transit=True)

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
        )
    )

    assert evidence.days[0].route_legs
    assert all(leg.mode == "driving" and leg.is_fallback for leg in evidence.days[0].route_legs)
    modes = [query.mode for query in client.route_queries]
    assert "transit" in modes
    assert "driving" in modes


@pytest.mark.asyncio
async def test_all_route_failures_keep_order_with_straight_line_estimates() -> None:
    client = FakeMapClient(attractions=standard_attractions()[:3], route_fails=True)

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
        )
    )

    day = evidence.days[0]
    assert all(leg.mode == "estimated" and leg.is_fallback for leg in day.route_legs)
    assert all(leg.distance_meters and leg.duration_seconds for leg in day.route_legs)
    assert any("打开高德" in warning for warning in evidence.warnings)
    assert "provider details" not in " ".join(leg.route_summary or "" for leg in day.route_legs)


@pytest.mark.asyncio
async def test_partial_poi_query_failure_uses_other_successful_results() -> None:
    client = FakeMapClient(
        attractions=standard_attractions(),
        failed_keywords={"博物馆"},
    )

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
        )
    )

    assert len(evidence.days[0].attractions) >= 3
    assert any("博物馆" in warning for warning in evidence.warnings)


@pytest.mark.asyncio
async def test_partial_poi_timeout_keeps_completed_query_results() -> None:
    class SlowKeywordClient(FakeMapClient):
        async def search_places(self, query: SearchPlacesInput) -> PlaceSearchResult:
            if str(query.keywords) == "风景名胜":
                await asyncio.sleep(10)
            return await super().search_places(query)

    client = SlowKeywordClient(attractions=standard_attractions())

    evidence = await MapTripCollectionService(client, data_timeout_seconds=0.05).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
        )
    )

    assert len(evidence.days[0].attractions) >= 3
    assert any("风景名胜" in warning and "超时" in warning for warning in evidence.warnings)


@pytest.mark.asyncio
async def test_single_valid_attraction_produces_a_degraded_day_without_route_calls() -> None:
    client = FakeMapClient(attractions=[place("only", "唯一景点", 0.001)])

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
        )
    )

    assert [item.poi_id for item in evidence.days[0].attractions] == ["only"]
    assert evidence.days[0].route_legs == []
    assert client.route_queries == []


@pytest.mark.asyncio
async def test_data_timeout_cancels_slow_routes_and_returns_estimates() -> None:
    class SlowRouteClient(FakeMapClient):
        async def plan_route(self, query: RoutePlanInput) -> RouteResult:
            self.route_queries.append(query)
            await asyncio.sleep(10)
            raise AssertionError("slow route should have been cancelled")

    client = SlowRouteClient(attractions=standard_attractions()[:3])

    evidence = await MapTripCollectionService(client, data_timeout_seconds=0.05).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
        )
    )

    assert all(leg.mode == "estimated" for leg in evidence.days[0].route_legs)
    assert any("数据阶段超时" in warning for warning in evidence.warnings)


@pytest.mark.asyncio
async def test_one_local_correction_swaps_an_abnormal_real_route_edge() -> None:
    attractions = [
        place("a", "景点A", 0.00),
        place("b", "景点B", 0.01),
        place("c", "景点C", 0.02),
        place("d", "景点D", 0.03),
    ]

    class CorrectableRouteClient(FakeMapClient):
        async def plan_route(self, query: RoutePlanInput) -> RouteResult:
            self.route_queries.append(query)
            is_abnormal = (
                round(query.origin.longitude, 2) == 104.01
                and round(query.destination.longitude, 2) == 104.02
            )
            return RouteResult(
                mode=query.mode,
                distance_meters=900,
                duration_seconds=6_000 if is_abnormal else 100,
                route_summary="异常路段" if is_abnormal else "正常路段",
                steps=[],
            )

    client = CorrectableRouteClient(attractions=attractions)

    evidence = await MapTripCollectionService(client).collect(
        CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
        )
    )

    assert [item.poi_id for item in evidence.days[0].attractions] == ["a", "c", "b", "d"]
    assert len(client.route_queries) == 6
