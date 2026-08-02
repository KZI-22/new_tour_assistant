from __future__ import annotations

from typing import Any

import pytest
from app.schemas.amap import AmapCoordinate, AmapPlace, PlaceSearchResult
from app.services.restaurant_recommendation_service import RestaurantRecommendationService


def _place(
    poi_id: str,
    name: str,
    *,
    rating: float | None,
    poi_type: str = "餐饮服务;中餐厅",
) -> AmapPlace:
    return AmapPlace(
        poi_id=poi_id,
        name=name,
        address="测试路 1 号",
        province="浙江省",
        city="杭州市",
        district="上城区",
        adcode="330102",
        poi_type=poi_type,
        rating=rating,
        business_area="湖滨",
        location=AmapCoordinate(longitude=120.16, latitude=30.25),
    )


class FakeRestaurantClient:
    async def search_places(self, query: Any) -> PlaceSearchResult:
        common = _place("food-1", "杭州味道", rating=4.8)
        if query.keywords == "本地特色美食":
            return PlaceSearchResult(
                pois=[
                    common,
                    _place("food-2", "江南小馆", rating=4.6),
                    _place("not-food", "西湖景区", rating=4.9, poi_type="风景名胜"),
                ]
            )
        if query.keywords == "老字号餐厅":
            return PlaceSearchResult(pois=[common, _place("food-3", "百年老店", rating=4.5)])
        return PlaceSearchResult(pois=[_place("food-4", "城市餐厅", rating=4.4)])


@pytest.mark.asyncio
async def test_restaurant_recommendations_are_deduplicated_ranked_and_capped() -> None:
    evidence = await RestaurantRecommendationService(FakeRestaurantClient()).collect("杭州")

    assert evidence.status == "usable"
    assert len(evidence.recommendations) == 3
    assert evidence.recommendations[0].provider_place_id == "food-1"
    assert len({item.provider_place_id for item in evidence.recommendations}) == 3
    assert all("餐饮" in item.poi_type for item in evidence.recommendations)
    assert "多组餐饮关键词" in evidence.recommendations[0].recommendation_reason


@pytest.mark.asyncio
async def test_missing_amap_is_optional_for_core_plan() -> None:
    evidence = await RestaurantRecommendationService(None).collect("杭州")

    assert evidence.status == "failed"
    assert evidence.recommendations == []
    assert evidence.error_code == "AMAP_UNAVAILABLE"
