from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from app.schemas.amap import AmapPlace, PlaceSearchResult, SearchPlacesInput
from app.schemas.platform_planning import (
    RestaurantRecommendation,
    RestaurantSearchEvidence,
    RestaurantSearchInput,
)

_RECOMMENDATION_QUERIES = ("本地特色美食", "老字号餐厅", "特色餐厅")
_DINING_TYPE_MARKERS = ("餐饮", "餐厅", "小吃", "甜品", "咖啡")


class RestaurantPlanningClient(Protocol):
    async def search_places(self, query: SearchPlacesInput) -> PlaceSearchResult: ...


@dataclass(slots=True)
class _RestaurantCandidate:
    place: AmapPlace
    query_ranks: dict[str, int] = field(default_factory=dict)

    @property
    def best_rank(self) -> int:
        return min(self.query_ranks.values())


async def search_restaurant_places(
    client: RestaurantPlanningClient,
    query: RestaurantSearchInput,
) -> PlaceSearchResult:
    result = await client.search_places(
        SearchPlacesInput(
            keywords=query.keyword,
            city=query.city,
            poi_type="餐饮服务",
            limit=query.limit,
        )
    )
    return PlaceSearchResult(
        pois=[place for place in result.pois if _is_restaurant(place, query.city)]
    )


class RestaurantRecommendationService:
    def __init__(self, client: RestaurantPlanningClient | None) -> None:
        self._client = client

    async def collect(self, city: str) -> RestaurantSearchEvidence:
        queried_at = datetime.now(UTC)
        if self._client is None:
            return RestaurantSearchEvidence(
                status="failed",
                queried_at=queried_at,
                warnings=["餐饮推荐暂不可用。"],
                error_code="AMAP_UNAVAILABLE",
            )

        tasks = [
            asyncio.create_task(
                search_restaurant_places(
                    self._client,
                    RestaurantSearchInput(city=city, keyword=keyword, limit=10),
                )
            )
            for keyword in _RECOMMENDATION_QUERIES
        ]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            await _cancel_tasks(tasks)
            raise

        candidates: dict[str, _RestaurantCandidate] = {}
        failures = 0
        for keyword, result in zip(_RECOMMENDATION_QUERIES, results, strict=True):
            if isinstance(result, BaseException):
                failures += 1
                continue
            for rank, place in enumerate(result.pois, start=1):
                candidate = candidates.setdefault(
                    place.poi_id,
                    _RestaurantCandidate(place=place),
                )
                candidate.query_ranks[keyword] = min(
                    rank,
                    candidate.query_ranks.get(keyword, rank),
                )

        if not candidates:
            return RestaurantSearchEvidence(
                status="failed" if failures == len(tasks) else "empty",
                queried_at=queried_at,
                warnings=[
                    "餐饮数据查询失败，未影响核心行程。"
                    if failures == len(tasks)
                    else "未找到信息完整的餐饮推荐。"
                ],
                error_code="AMAP_SEARCH_FAILED" if failures == len(tasks) else None,
            )

        ranked = sorted(candidates.values(), key=_candidate_sort_key)
        recommendations = [_recommendation(item) for item in ranked[:3]]
        warnings = ["部分餐饮关键词查询失败。"] if failures else []
        return RestaurantSearchEvidence(
            status="usable",
            queried_at=queried_at,
            recommendations=recommendations,
            warnings=warnings,
        )


def _candidate_sort_key(candidate: _RestaurantCandidate) -> tuple[float, int, int, str]:
    return (
        -(candidate.place.rating if candidate.place.rating is not None else -1),
        candidate.best_rank,
        -len(candidate.query_ranks),
        candidate.place.name,
    )


def _recommendation(candidate: _RestaurantCandidate) -> RestaurantRecommendation:
    place = candidate.place
    reasons: list[str] = []
    if place.rating is not None:
        reasons.append(f"高德评分 {place.rating:g}")
    if len(candidate.query_ranks) > 1:
        reasons.append("在多组餐饮关键词中重复出现")
    if candidate.best_rank <= 3:
        reasons.append("高德关键词检索排名靠前")
    if place.business_area:
        reasons.append(f"位于{place.business_area}")
    if not reasons:
        reasons.append("来自高德餐饮分类检索结果")
    return RestaurantRecommendation(
        provider_place_id=place.poi_id,
        name=place.name,
        address=place.address,
        poi_type=place.poi_type,
        rating=place.rating,
        business_area=place.business_area,
        city=place.city or None,
        adcode=place.adcode or None,
        location=place.location,
        source_queries=list(candidate.query_ranks),
        best_search_rank=candidate.best_rank,
        selection_reasons=reasons,
        recommendation_reason="；".join(reasons) + "。",
    )


def _is_restaurant(place: AmapPlace, city: str) -> bool:
    if place.city and not _same_city(place.city, city):
        return False
    text = f"{place.poi_type};{place.name}"
    return any(marker in text for marker in _DINING_TYPE_MARKERS)


def _same_city(left: str, right: str) -> bool:
    normalized_left = left.strip().removesuffix("市").casefold()
    normalized_right = right.strip().removesuffix("市").casefold()
    return normalized_left == normalized_right


async def _cancel_tasks(tasks: Sequence[asyncio.Task[object]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


__all__ = [
    "RestaurantPlanningClient",
    "RestaurantRecommendationService",
    "search_restaurant_places",
]
