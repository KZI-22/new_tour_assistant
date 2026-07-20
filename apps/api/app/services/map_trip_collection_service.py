from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.clients.amap_errors import AmapError
from app.schemas.amap import (
    AmapCoordinate,
    AmapCoordinateInput,
    AmapPlace,
    MatrixEntry,
    MatrixLocation,
    MatrixMode,
    PlaceSearchResult,
    RouteMode,
    RoutePlanInput,
    RouteResult,
    SearchPlacesInput,
    TravelTimeMatrixInput,
    TravelTimeMatrixResult,
)
from app.schemas.map_planning import (
    MapDayEvidence,
    MapPlaceEvidence,
    MapPlaceRole,
    MapTripEvidence,
    RouteLegEvidence,
)
from app.schemas.trip_planning import CityTripRequest

logger = logging.getLogger(__name__)

_MAX_MATRIX_LOCATIONS = 20
_MAX_ATTRACTION_CANDIDATES = 20
_MEAL_CANDIDATE_LIMIT = 5
_WALKING_DISTANCE_LIMIT_METERS = 1_500
_WALKING_DURATION_LIMIT_SECONDS = 20 * 60
_EXCLUDED_ATTRACTION_TYPES = ("餐饮", "酒店", "住宿", "购物", "生活服务", "汽车服务")


class MapPlanningClient(Protocol):
    async def search_places(self, query: SearchPlacesInput) -> PlaceSearchResult: ...

    async def travel_time_matrix(
        self,
        query: TravelTimeMatrixInput,
    ) -> TravelTimeMatrixResult: ...

    async def plan_route(self, query: RoutePlanInput) -> RouteResult: ...


class MapTripCollectionError(RuntimeError):
    def __init__(self, code: str, user_message: str) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


@dataclass(frozen=True, slots=True)
class _Candidate:
    place: AmapPlace
    search_query: str
    search_rank: int
    source_order: int
    interest_match: bool = False


@dataclass(frozen=True, slots=True)
class _DayAttractions:
    morning: _Candidate
    afternoon: _Candidate | None


@dataclass(frozen=True, slots=True)
class _MealCandidates:
    breakfast: list[_Candidate]
    lunch: list[_Candidate]
    dinner: list[_Candidate]
    warnings: list[str]


class MapTripCollectionService:
    def __init__(self, client: MapPlanningClient | None) -> None:
        self._client = client

    async def collect(self, request: CityTripRequest) -> MapTripEvidence:
        if (
            self._client is None
            or not request.destination_city
            or request.duration_days is None
            or request.start_date is None
        ):
            raise MapTripCollectionError(
                "MAP_PLANNING_UNAVAILABLE",
                "地图规划服务未配置，暂时无法生成地图与天气方案。",
            )

        queried_at = datetime.now(UTC)
        candidates, warnings = await self._collect_attractions(request)
        if not candidates:
            raise MapTripCollectionError(
                "MAP_ATTRACTIONS_EMPTY",
                "高德地图没有返回可用于规划的有效景点，请换个城市名称后重试。",
            )
        attraction_matrix = await self._matrix(candidates)
        days = self._group_attractions(
            candidates,
            attraction_matrix,
            request.duration_days,
        )
        if len(days) < request.duration_days:
            warnings.append(
                f"有效景点仅能覆盖 {len(days)}/{request.duration_days} 个行程日，"
                "未使用模型猜测地点补足。"
            )
        if any(day.afternoon is None for day in days):
            warnings.append("部分行程日只有一个有效景点，未添加无来源的下午景点。")

        meal_results = await asyncio.gather(
            *(self._collect_meals(request, day) for day in days),
        )
        used_restaurants: set[str] = set()
        evidence_days: list[MapDayEvidence] = []
        for index, (attractions, meals) in enumerate(
            zip(days, meal_results, strict=True),
            start=1,
        ):
            day, day_warnings = await self._build_day(
                request=request,
                day_index=index,
                attractions=attractions,
                meals=meals,
                used_restaurants=used_restaurants,
            )
            evidence_days.append(day)
            warnings.extend(day_warnings)

        for index in range(len(evidence_days) + 1, request.duration_days + 1):
            warning = f"第 {index} 天没有可用的高德景点证据，保留为空白规划日。"
            evidence_days.append(
                MapDayEvidence(
                    day_index=index,
                    date=request.start_date + timedelta(days=index - 1),
                    warnings=[warning],
                )
            )
            warnings.append(warning)

        return MapTripEvidence(
            city=request.destination_city,
            queried_at=queried_at,
            days=evidence_days,
            warnings=list(dict.fromkeys(warnings)),
        )

    async def _collect_attractions(
        self,
        request: CityTripRequest,
    ) -> tuple[list[_Candidate], list[str]]:
        assert self._client is not None
        assert request.destination_city is not None
        assert request.duration_days is not None
        queries = ["景点", *request.interests[:2]]
        limit = min(
            _MAX_ATTRACTION_CANDIDATES,
            max(request.duration_days * 3, 6),
        )
        results = await asyncio.gather(
            *(
                self._safe_search(
                    SearchPlacesInput(
                        city=request.destination_city,
                        keywords=query,
                        limit=limit,
                    ),
                    label=f"景点关键词“{query}”",
                )
                for query in queries
            )
        )
        warnings = [warning for _, warning in results if warning]
        candidates: list[_Candidate] = []
        seen: set[str] = set()
        source_order = 0
        for query_index, (query, (places, _)) in enumerate(zip(queries, results, strict=True)):
            for rank, place in enumerate(places, start=1):
                if place.poi_id in seen or not _is_valid_attraction(
                    place,
                    request.destination_city,
                ):
                    continue
                seen.add(place.poi_id)
                source_order += 1
                candidates.append(
                    _Candidate(
                        place=place,
                        search_query=query,
                        search_rank=rank,
                        source_order=source_order,
                        interest_match=query_index > 0,
                    )
                )
                if len(candidates) == _MAX_ATTRACTION_CANDIDATES:
                    return candidates, warnings
        return candidates, warnings

    async def _collect_meals(
        self,
        request: CityTripRequest,
        attractions: _DayAttractions,
    ) -> _MealCandidates:
        morning = attractions.morning.place.location
        afternoon = (attractions.afternoon or attractions.morning).place.location
        midpoint = AmapCoordinateInput(
            longitude=(morning.longitude + afternoon.longitude) / 2,
            latitude=(morning.latitude + afternoon.latitude) / 2,
        )
        preference = request.food_preferences[0] if request.food_preferences else None
        tasks = (
            self._meal_search(
                city=request.destination_city or "",
                keyword="早餐",
                center=morning,
                radius_meters=1_500,
                role_label="早餐",
            ),
            self._meal_search(
                city=request.destination_city or "",
                keyword=f"{preference} 餐厅" if preference else "餐厅",
                center=midpoint,
                radius_meters=2_500,
                role_label="午餐",
            ),
            self._meal_search(
                city=request.destination_city or "",
                keyword=f"{preference} 晚餐" if preference else "晚餐",
                center=afternoon,
                radius_meters=1_500,
                role_label="晚餐",
            ),
        )
        breakfast, lunch, dinner = await asyncio.gather(*tasks)
        return _MealCandidates(
            breakfast=breakfast[0],
            lunch=lunch[0],
            dinner=dinner[0],
            warnings=[warning for _, warning in (breakfast, lunch, dinner) if warning],
        )

    async def _meal_search(
        self,
        *,
        city: str,
        keyword: str,
        center: AmapCoordinate | AmapCoordinateInput,
        radius_meters: int,
        role_label: str,
    ) -> tuple[list[_Candidate], str | None]:
        query = SearchPlacesInput(
            city=city,
            keywords=keyword,
            location=AmapCoordinateInput(
                longitude=center.longitude,
                latitude=center.latitude,
            ),
            radius_meters=radius_meters,
            limit=_MEAL_CANDIDATE_LIMIT,
        )
        places, warning = await self._safe_search(query, label=role_label)
        search_query = keyword
        if not places:
            retry = query.model_copy(update={"keywords": "餐饮"})
            places, retry_warning = await self._safe_search(retry, label=f"{role_label}宽泛重试")
            warning = warning or retry_warning
            search_query = "餐饮"
        candidates = [
            _Candidate(
                place=place,
                search_query=search_query,
                search_rank=rank,
                source_order=rank,
            )
            for rank, place in enumerate(_unique_valid_places(places), start=1)
        ]
        if candidates:
            return candidates, warning
        return [], f"{role_label}未找到有效高德 POI，保留现场选择提示。"

    async def _build_day(
        self,
        *,
        request: CityTripRequest,
        day_index: int,
        attractions: _DayAttractions,
        meals: _MealCandidates,
        used_restaurants: set[str],
    ) -> tuple[MapDayEvidence, list[str]]:
        attraction_ids = {
            attractions.morning.place.poi_id,
            *([attractions.afternoon.place.poi_id] if attractions.afternoon is not None else []),
        }
        breakfast = _exclude_used(meals.breakfast, used_restaurants | attraction_ids)
        lunch = _exclude_used(meals.lunch, used_restaurants | attraction_ids)
        dinner = _exclude_used(meals.dinner, used_restaurants | attraction_ids)
        all_candidates = _unique_candidates(
            [
                *breakfast,
                attractions.morning,
                *lunch,
                *([attractions.afternoon] if attractions.afternoon else []),
                *dinner,
            ]
        )
        matrix = await self._matrix(all_candidates)
        selected = self._select_meals(
            breakfast=breakfast,
            morning=attractions.morning,
            lunch=lunch,
            afternoon=attractions.afternoon,
            dinner=dinner,
            matrix=matrix,
        )
        warnings = list(meals.warnings)
        if selected is None:
            selected_breakfast = selected_lunch = selected_dinner = None
            warnings.append(f"第 {day_index} 天餐饮候选缺少完整步行矩阵，未按猜测距离推荐餐厅。")
        else:
            selected_breakfast, selected_lunch, selected_dinner = selected
            used_restaurants.update(item.place.poi_id for item in selected if item is not None)

        places = {
            "breakfast": _evidence(selected_breakfast, day_index, "breakfast"),
            "morning_attraction": _evidence(
                attractions.morning,
                day_index,
                "morning_attraction",
            ),
            "lunch": _evidence(selected_lunch, day_index, "lunch"),
            "afternoon_attraction": _evidence(
                attractions.afternoon,
                day_index,
                "afternoon_attraction",
            ),
            "dinner": _evidence(selected_dinner, day_index, "dinner"),
        }
        ordered = [item for item in places.values() if item is not None]
        route_legs = await asyncio.gather(
            *(
                self._route_leg(
                    origin,
                    destination,
                    matrix,
                    request.destination_city or "",
                )
                for origin, destination in itertools.pairwise(ordered)
            )
        )
        if any(leg.mode == "unverified" for leg in route_legs):
            warnings.append(f"第 {day_index} 天存在未验证路段，出发前请使用地图导航复查。")
        assert request.start_date is not None
        return (
            MapDayEvidence(
                day_index=day_index,
                date=request.start_date + timedelta(days=day_index - 1),
                breakfast=places["breakfast"],
                morning_attraction=places["morning_attraction"],
                lunch=places["lunch"],
                afternoon_attraction=places["afternoon_attraction"],
                dinner=places["dinner"],
                route_legs=list(route_legs),
                warnings=list(dict.fromkeys(warnings)),
            ),
            warnings,
        )

    def _select_meals(
        self,
        *,
        breakfast: list[_Candidate],
        morning: _Candidate,
        lunch: list[_Candidate],
        afternoon: _Candidate | None,
        dinner: list[_Candidate],
        matrix: TravelTimeMatrixResult | None,
    ) -> tuple[_Candidate | None, _Candidate | None, _Candidate | None] | None:
        if matrix is None:
            return None
        lookup = _matrix_lookup(matrix)
        choices = (
            breakfast or [None],
            lunch or [None],
            dinner or [None],
        )
        best: tuple[float, tuple[str, str, str], tuple[_Candidate | None, ...]] | None = None
        for meal_combo in itertools.product(*choices):
            sequence = [meal_combo[0], morning, meal_combo[1], afternoon, meal_combo[2]]
            actual = [item for item in sequence if item is not None]
            ids = [item.place.poi_id for item in actual]
            if len(ids) != len(set(ids)):
                continue
            entries = [
                lookup.get((origin.place.poi_id, destination.place.poi_id))
                for origin, destination in itertools.pairwise(actual)
            ]
            if any(entry is None or not entry.success for entry in entries):
                continue
            cost = 0.0
            for entry in entries:
                assert entry is not None
                assert entry.distance_meters is not None and entry.duration_seconds is not None
                cost += entry.duration_seconds + entry.distance_meters / 1.4
                if (
                    entry.distance_meters > _WALKING_DISTANCE_LIMIT_METERS
                    or entry.duration_seconds > _WALKING_DURATION_LIMIT_SECONDS
                ):
                    cost += 100_000
            cost += sum(item.search_rank * 10 for item in meal_combo if item is not None)
            stable = tuple(item.place.poi_id if item is not None else "" for item in meal_combo)
            candidate = (cost, stable, meal_combo)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            return None
        selected = best[2]
        return selected[0], selected[1], selected[2]

    async def _route_leg(
        self,
        origin: MapPlaceEvidence,
        destination: MapPlaceEvidence,
        matrix: TravelTimeMatrixResult | None,
        city: str,
    ) -> RouteLegEvidence:
        entry = (
            _matrix_lookup(matrix).get((origin.poi_id, destination.poi_id))
            if matrix is not None
            else None
        )
        if entry is None or not entry.success:
            return RouteLegEvidence(
                origin_ref=origin.reference_id,
                destination_ref=destination.reference_id,
                mode="unverified",
                route_summary="高德未返回该路段的有效距离，请在出发前使用地图导航确认。",
            )
        assert entry.distance_meters is not None and entry.duration_seconds is not None
        if (
            entry.distance_meters <= _WALKING_DISTANCE_LIMIT_METERS
            and entry.duration_seconds <= _WALKING_DURATION_LIMIT_SECONDS
        ):
            return RouteLegEvidence(
                origin_ref=origin.reference_id,
                destination_ref=destination.reference_id,
                mode="walking",
                distance_meters=entry.distance_meters,
                duration_seconds=entry.duration_seconds,
                route_summary="高德步行时间矩阵",
            )
        assert self._client is not None
        try:
            route = await self._client.plan_route(
                RoutePlanInput(
                    origin=_coordinate_input(origin.location),
                    destination=_coordinate_input(destination.location),
                    mode=RouteMode.TRANSIT,
                    city=city,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Amap transit route query failed exception_type=%s",
                type(exc).__name__,
            )
            return RouteLegEvidence(
                origin_ref=origin.reference_id,
                destination_ref=destination.reference_id,
                mode="unverified",
                distance_meters=entry.distance_meters,
                duration_seconds=entry.duration_seconds,
                route_summary="公交路线查询失败；距离和耗时为已知步行矩阵结果。",
            )
        return RouteLegEvidence(
            origin_ref=origin.reference_id,
            destination_ref=destination.reference_id,
            mode="transit",
            distance_meters=route.distance_meters,
            duration_seconds=route.duration_seconds,
            route_summary=route.route_summary,
        )

    async def _matrix(
        self,
        candidates: list[_Candidate],
    ) -> TravelTimeMatrixResult | None:
        assert self._client is not None
        unique = _unique_candidates(candidates)[:_MAX_MATRIX_LOCATIONS]
        if len(unique) < 2:
            return None
        try:
            return await self._client.travel_time_matrix(
                TravelTimeMatrixInput(
                    locations=[
                        MatrixLocation(
                            id=item.place.poi_id,
                            name=item.place.name,
                            longitude=item.place.location.longitude,
                            latitude=item.place.location.latitude,
                        )
                        for item in unique
                    ],
                    mode=MatrixMode.WALKING,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Amap walking matrix query failed exception_type=%s",
                type(exc).__name__,
            )
            return None

    def _group_attractions(
        self,
        candidates: list[_Candidate],
        matrix: TravelTimeMatrixResult | None,
        duration_days: int,
    ) -> list[_DayAttractions]:
        used: set[str] = set()
        days: list[_DayAttractions] = []
        lookup = _matrix_lookup(matrix) if matrix is not None else {}
        scored_pairs: list[tuple[float, int, int, _Candidate, _Candidate]] = []
        for left, right in itertools.combinations(candidates, 2):
            entries = [
                lookup.get((left.place.poi_id, right.place.poi_id)),
                lookup.get((right.place.poi_id, left.place.poi_id)),
            ]
            valid = [entry for entry in entries if entry is not None and entry.success]
            if not valid:
                continue
            distance = sum(entry.distance_meters or 0 for entry in valid) / len(valid)
            duration = sum(entry.duration_seconds or 0 for entry in valid) / len(valid)
            score = duration + distance / 1.4
            score += (left.source_order + right.source_order) * 120
            score -= (left.interest_match + right.interest_match) * 300
            scored_pairs.append((score, left.source_order, right.source_order, left, right))
        for _, _, _, left, right in sorted(scored_pairs, key=lambda item: item[:3]):
            if len(days) == duration_days:
                break
            if left.place.poi_id in used or right.place.poi_id in used:
                continue
            morning, afternoon = sorted((left, right), key=lambda item: item.source_order)
            used.update((morning.place.poi_id, afternoon.place.poi_id))
            days.append(_DayAttractions(morning=morning, afternoon=afternoon))
        for candidate in sorted(candidates, key=lambda item: item.source_order):
            if len(days) == duration_days:
                break
            if candidate.place.poi_id in used:
                continue
            used.add(candidate.place.poi_id)
            days.append(_DayAttractions(morning=candidate, afternoon=None))
        return days

    async def _safe_search(
        self,
        query: SearchPlacesInput,
        *,
        label: str,
    ) -> tuple[list[AmapPlace], str | None]:
        assert self._client is not None
        try:
            result = await self._client.search_places(query)
            return result.pois, None
        except asyncio.CancelledError:
            raise
        except AmapError as exc:
            logger.warning(
                "Amap place search failed label=%s error_code=%s",
                label,
                exc.error_code,
            )
        except Exception as exc:
            logger.warning(
                "Amap place search failed label=%s exception_type=%s",
                label,
                type(exc).__name__,
            )
        return [], f"{label}查询暂时不可用。"


def _is_valid_attraction(place: AmapPlace, destination_city: str) -> bool:
    if not place.poi_id or not place.name:
        return False
    if place.city and not _same_city(place.city, destination_city):
        return False
    normalized_type = place.poi_type.casefold()
    return not any(keyword.casefold() in normalized_type for keyword in _EXCLUDED_ATTRACTION_TYPES)


def _same_city(left: str, right: str) -> bool:
    def normalize(value: str) -> str:
        return value.strip().removesuffix("市").casefold()

    normalized_left = normalize(left)
    normalized_right = normalize(right)
    return (
        normalized_left == normalized_right
        or normalized_left in normalized_right
        or normalized_right in normalized_left
    )


def _unique_valid_places(places: list[AmapPlace]) -> list[AmapPlace]:
    unique: list[AmapPlace] = []
    seen: set[str] = set()
    for place in places:
        if not place.poi_id or not place.name or place.poi_id in seen:
            continue
        seen.add(place.poi_id)
        unique.append(place)
    return unique


def _unique_candidates(candidates: list[_Candidate]) -> list[_Candidate]:
    unique: list[_Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.place.poi_id in seen:
            continue
        seen.add(candidate.place.poi_id)
        unique.append(candidate)
    return unique


def _exclude_used(candidates: list[_Candidate], used: set[str]) -> list[_Candidate]:
    return [candidate for candidate in candidates if candidate.place.poi_id not in used]


def _matrix_lookup(
    matrix: TravelTimeMatrixResult,
) -> dict[tuple[str, str], MatrixEntry]:
    return {(entry.origin_id, entry.destination_id): entry for entry in matrix.matrix}


def _coordinate_input(coordinate: AmapCoordinate) -> AmapCoordinateInput:
    return AmapCoordinateInput(
        longitude=coordinate.longitude,
        latitude=coordinate.latitude,
    )


def _evidence(
    candidate: _Candidate | None,
    day_index: int,
    role: MapPlaceRole,
) -> MapPlaceEvidence | None:
    if candidate is None:
        return None
    place = candidate.place
    return MapPlaceEvidence(
        reference_id=f"day_{day_index}_{role}",
        role=role,
        poi_id=place.poi_id,
        name=place.name,
        address=place.address,
        poi_type=place.poi_type,
        location=place.location,
        adcode=place.adcode or None,
        city=place.city or None,
        search_query=candidate.search_query,
        search_rank=candidate.search_rank,
    )


__all__ = [
    "MapPlanningClient",
    "MapTripCollectionError",
    "MapTripCollectionService",
]
