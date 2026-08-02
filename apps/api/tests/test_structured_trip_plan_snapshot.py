from __future__ import annotations

from datetime import UTC, date, datetime

from app.schemas.amap import AmapCoordinate
from app.schemas.map_planning import MapDayEvidence, MapPlaceEvidence, MapTripEvidence
from app.schemas.platform_planning import (
    RestaurantRecommendation,
    RestaurantSearchEvidence,
    StructuredTripRequest,
)
from app.schemas.trip_evidence import MapWeatherEvidenceBundle
from app.schemas.trip_planning import DailyWeatherEvidence, TripWeatherEvidence
from app.services.trip_plan_snapshot_builder import build_structured_trip_plan_snapshot


def test_v2_snapshot_keeps_restaurants_outside_daily_route() -> None:
    queried_at = datetime(2026, 8, 2, tzinfo=UTC)
    place = MapPlaceEvidence(
        reference_id="d1-p1",
        poi_id="poi-1",
        name="西湖",
        address="西湖区",
        poi_type="风景名胜",
        location=AmapCoordinate(longitude=120.14, latitude=30.25),
        city="杭州市",
        search_query="自然景观",
        search_rank=1,
        estimated_visit_minutes=120,
        candidate_score=90,
    )
    map_weather = MapWeatherEvidenceBundle(
        status="usable",
        map=MapTripEvidence(
            city="杭州",
            planning_run_id="run-1",
            queried_at=queried_at,
            days=[
                MapDayEvidence(
                    day_index=1,
                    date=date(2026, 9, 1),
                    attractions=[place],
                    estimated_visit_minutes=120,
                )
            ],
        ),
        weather=TripWeatherEvidence(
            city="杭州",
            queried_at=queried_at,
            days=[DailyWeatherEvidence(date=date(2026, 9, 1), coverage="unavailable")],
        ),
    )
    restaurants = RestaurantSearchEvidence(
        status="usable",
        queried_at=queried_at,
        recommendations=[
            RestaurantRecommendation(
                provider_place_id="food-1",
                name="杭州味道",
                address="湖滨路",
                poi_type="餐饮服务;中餐厅",
                rating=4.8,
                location=AmapCoordinate(longitude=120.16, latitude=30.26),
                source_queries=["本地特色美食"],
                best_search_rank=1,
                recommendation_reason="高德评分 4.8。",
            )
        ],
    )

    snapshot = build_structured_trip_plan_snapshot(
        StructuredTripRequest(
            destination_city="杭州",
            start_date=date(2026, 9, 1),
            duration_days=1,
            interests=["自然风光"],
        ),
        map_weather,
        restaurants,
        planning_run_id="run-1",
    )

    assert snapshot.schema_version == "trip_plan.v2"
    assert [item.name for item in snapshot.days[0].places] == ["西湖"]
    assert snapshot.days[0].route_legs == []
    assert [item.name for item in snapshot.restaurant_recommendations] == ["杭州味道"]
