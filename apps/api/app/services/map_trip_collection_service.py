from __future__ import annotations

import asyncio
import itertools
import logging
import statistics
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Protocol
from uuid import uuid4

from app.clients.amap_errors import AmapError
from app.schemas.amap import (
    AmapCoordinateInput,
    AmapPlace,
    PlaceSearchResult,
    RouteMode,
    RoutePlanInput,
    RouteResult,
    SearchPlacesInput,
)
from app.schemas.map_planning import (
    ExcludedAttractionEvidence,
    MapDayEvidence,
    MapPlaceEvidence,
    MapTripEvidence,
    RouteLegEvidence,
)
from app.schemas.trip_planning import CityTripRequest
from app.services.attraction_planning_service import (
    AttractionCandidate,
    DailyClusterPlanner,
    PoiSearchTask,
    build_poi_search_tasks,
    haversine_km,
    match_candidate_preferences,
    merge_and_deduplicate_candidates,
    score_candidates,
    select_diverse_candidates,
    straight_line_transport_minutes,
)

logger = logging.getLogger(__name__)

_WALKING_THRESHOLD_KM = 1.5
_LOCAL_ROUTE_ABNORMAL_SECONDS = 90 * 60
_LOCAL_ROUTE_CORRECTION_MAX_NEW_EDGES = 6


class MapPlanningClient(Protocol):
    async def search_places(self, query: SearchPlacesInput) -> PlaceSearchResult: ...

    async def plan_route(self, query: RoutePlanInput) -> RouteResult: ...


class MapTripCollectionError(RuntimeError):
    def __init__(self, code: str, user_message: str) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


class MapTripCollectionService:
    def __init__(
        self,
        client: MapPlanningClient | None,
        *,
        poi_max_concurrency: int = 5,
        route_max_concurrency: int = 5,
        poi_page_size: int = 10,
        max_raw_candidates: int = 60,
        max_transit_transfers: int = 1,
        max_transit_duration_minutes: int = 90,
        max_walk_distance_meters: int = 1_800,
        cluster_max_iterations: int = 20,
        data_timeout_seconds: float = 10,
    ) -> None:
        positive = (
            poi_max_concurrency,
            route_max_concurrency,
            poi_page_size,
            max_raw_candidates,
            max_transit_duration_minutes,
            max_walk_distance_meters,
            data_timeout_seconds,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("Map planning limits and timeouts must be positive")
        if max_transit_transfers < 0 or cluster_max_iterations < 0:
            raise ValueError("Map planning limits cannot be negative")
        self._client = client
        self._poi_semaphore = asyncio.Semaphore(poi_max_concurrency)
        self._route_semaphore = asyncio.Semaphore(route_max_concurrency)
        self._poi_page_size = poi_page_size
        self._max_raw_candidates = max_raw_candidates
        self._max_transit_transfers = max_transit_transfers
        self._max_transit_duration_minutes = max_transit_duration_minutes
        self._max_walk_distance_meters = max_walk_distance_meters
        self._cluster_planner = DailyClusterPlanner(max_iterations=cluster_max_iterations)
        self._data_timeout_seconds = data_timeout_seconds

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

        started_at = monotonic()
        planning_run_id = str(uuid4())
        queried_at = datetime.now(UTC)
        candidates, warnings, stats = await self._collect_attractions(
            request,
            planning_run_id,
            timeout_seconds=self._data_timeout_seconds,
        )
        if not candidates:
            timed_out = any("超时" in warning for warning in warnings)
            raise MapTripCollectionError(
                "MAP_POI_COLLECTION_TIMEOUT" if timed_out else "MAP_ATTRACTIONS_EMPTY",
                (
                    "高德景点查询超时，暂时无法形成可靠的候选景点。"
                    if timed_out
                    else "高德地图没有返回可用于规划的有效景点，请换个城市名称后重试。"
                ),
            )

        match_candidate_preferences(candidates, request.interests)
        score_candidates(candidates)
        selected, excluded = select_diverse_candidates(candidates, request.duration_days)
        if not selected:
            raise MapTripCollectionError(
                "MAP_ATTRACTIONS_EMPTY",
                "高德地图没有返回可用于规划的有效景点，请换个城市名称后重试。",
            )
        groups = self._cluster_planner.plan(selected, request.duration_days)
        if len(selected) < request.duration_days * 3:
            warnings.append(
                f"有效景点仅有 {len(selected)} 个，少于每天 3 个的目标；未使用模型猜测地点补足。"
            )

        remaining_timeout = max(
            0.0,
            self._data_timeout_seconds - (monotonic() - started_at),
        )
        route_groups, route_warnings, route_stats = await self._collect_routes(
            groups,
            request.destination_city,
            planning_run_id,
            timeout_seconds=remaining_timeout,
        )
        warnings.extend(route_warnings)

        evidence_days = [
            self._build_day(
                request=request,
                day_index=index,
                attractions=attractions,
                legs=legs,
            )
            for index, (attractions, legs) in enumerate(route_groups, start=1)
        ]
        evidence = MapTripEvidence(
            city=request.destination_city,
            planning_run_id=planning_run_id,
            queried_at=queried_at,
            days=evidence_days,
            excluded_attractions=[
                ExcludedAttractionEvidence(
                    poi_id=item.place.poi_id,
                    name=item.place.name,
                    reason=_exclusion_reason(item, selected),
                )
                for item in sorted(excluded, key=lambda candidate: -candidate.score)[:20]
            ],
            warnings=list(dict.fromkeys(warnings)),
        )
        elapsed_ms = round((monotonic() - started_at) * 1_000)
        logger.info(
            "Map trip data planned planning_run_id=%s city=%s raw_poi_count=%s "
            "invalid_poi_count=%s exact_duplicate_count=%s fuzzy_duplicate_count=%s "
            "deduplicated_poi_count=%s selected_poi_count=%s route_api_call_count=%s "
            "route_fallback_count=%s planning_total_latency_ms=%s",
            planning_run_id,
            request.destination_city,
            stats["raw"],
            stats["invalid"],
            stats["exact_duplicates"],
            stats["fuzzy_duplicates"],
            len(candidates),
            len(selected),
            route_stats["api_calls"],
            route_stats["fallbacks"],
            elapsed_ms,
        )
        return evidence

    async def _collect_attractions(
        self,
        request: CityTripRequest,
        planning_run_id: str,
        *,
        timeout_seconds: float,
    ) -> tuple[list[AttractionCandidate], list[str], dict[str, int]]:
        assert request.destination_city is not None
        deadline = monotonic() + timeout_seconds
        tasks = build_poi_search_tasks(request.interests)
        running = {
            asyncio.create_task(
                self._safe_search(task, request.destination_city, planning_run_id)
            ): task
            for task in tasks
        }
        try:
            done, pending = await asyncio.wait(running, timeout=timeout_seconds)
        except asyncio.CancelledError:
            for future in running:
                future.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            raise
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        responses = {running[future]: future.result() for future in done}
        warnings = [warning for _, warning in responses.values() if warning]
        warnings.extend(
            f"景点关键词“{running[future].keyword}”查询超时。" for future in pending
        )
        successful_results = [
            (task, places)
            for task in tasks
            if task in responses
            for places, _ in [responses[task]]
            if places
        ]
        if warnings and sum(len(places) for _, places in successful_results) < (
            request.duration_days or 1
        ) * 3:
            compensation = PoiSearchTask(keyword="旅游景点", is_base=True)
            remaining_seconds = deadline - monotonic()
            if remaining_seconds <= 0.05:
                places, warning = [], "旅游景点补偿查询因数据阶段超时未执行。"
            else:
                try:
                    places, warning = await asyncio.wait_for(
                        self._safe_search(
                            compensation,
                            request.destination_city,
                            planning_run_id,
                        ),
                        timeout=remaining_seconds,
                    )
                except TimeoutError:
                    places, warning = [], "旅游景点补偿查询超时。"
            if places:
                successful_results.append((compensation, places))
            if warning:
                warnings.append(warning)

        limited: list[tuple[PoiSearchTask, list[AmapPlace]]] = []
        remaining = self._max_raw_candidates
        for task, places in successful_results:
            if remaining <= 0:
                break
            current = list(places[:remaining])
            limited.append((task, current))
            remaining -= len(current)
        candidates, stats = merge_and_deduplicate_candidates(
            request.destination_city,
            limited,
        )
        return candidates, warnings, stats

    async def _safe_search(
        self,
        task: PoiSearchTask,
        city: str,
        planning_run_id: str,
    ) -> tuple[list[AmapPlace], str | None]:
        assert self._client is not None
        started_at = monotonic()
        try:
            async with self._poi_semaphore:
                result = await self._client.search_places(
                    SearchPlacesInput(
                        city=city,
                        keywords=task.keyword,
                        limit=self._poi_page_size,
                    )
                )
            places = list(result.pois[: self._poi_page_size])
            logger.info(
                "Amap POI search completed planning_run_id=%s keyword=%s "
                "result_count=%s poi_search_latency_ms=%s",
                planning_run_id,
                task.keyword,
                len(places),
                round((monotonic() - started_at) * 1_000),
            )
            return places, None
        except asyncio.CancelledError:
            raise
        except AmapError as exc:
            logger.warning(
                "Amap place search failed planning_run_id=%s keyword=%s error_code=%s "
                "poi_search_latency_ms=%s",
                planning_run_id,
                task.keyword,
                exc.error_code,
                round((monotonic() - started_at) * 1_000),
            )
        except Exception as exc:
            logger.warning(
                "Amap place search failed planning_run_id=%s keyword=%s exception_type=%s "
                "poi_search_latency_ms=%s",
                planning_run_id,
                task.keyword,
                type(exc).__name__,
                round((monotonic() - started_at) * 1_000),
            )
        return [], f"景点关键词“{task.keyword}”查询暂时不可用。"

    async def _collect_routes(
        self,
        groups: list[list[AttractionCandidate]],
        city: str,
        planning_run_id: str,
        *,
        timeout_seconds: float,
    ) -> tuple[
        list[tuple[list[AttractionCandidate], list[RouteLegEvidence]]],
        list[str],
        dict[str, int],
    ]:
        pairs = [
            (day_index, origin, destination)
            for day_index, group in enumerate(groups)
            for origin, destination in itertools.pairwise(group)
        ]
        if not pairs:
            return (
                [(group, []) for group in groups],
                [],
                {"api_calls": 0, "fallbacks": 0},
            )
        deadline = monotonic() + timeout_seconds
        tasks = {
            asyncio.create_task(
                self._route_leg(origin, destination, city, planning_run_id)
            ): (
                day_index,
                origin,
                destination,
            )
            for day_index, origin, destination in pairs
        }
        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        cache: dict[tuple[str, str], RouteLegEvidence] = {}
        api_calls = 0
        fallback_count = 0
        for task, (_, origin, destination) in tasks.items():
            key = (origin.place.poi_id, destination.place.poi_id)
            if task in done:
                try:
                    leg, calls = task.result()
                except Exception:
                    leg, calls = _estimated_leg(origin, destination), 0
                api_calls += calls
            else:
                leg = _estimated_leg(origin, destination, timed_out=True)
                calls = 0
            cache[key] = leg
            fallback_count += int(leg.is_fallback)

        correction_budget = _LOCAL_ROUTE_CORRECTION_MAX_NEW_EDGES
        corrected_groups: list[tuple[list[AttractionCandidate], list[RouteLegEvidence]]] = []
        for group in groups:
            legs = [
                cache[(origin.place.poi_id, destination.place.poi_id)]
                for origin, destination in itertools.pairwise(group)
            ]
            remaining_seconds = deadline - monotonic()
            if pending or remaining_seconds <= 0.05:
                corrected_group, corrected_legs, new_calls = group, legs, 0
            else:
                try:
                    corrected_group, corrected_legs, new_calls = await asyncio.wait_for(
                        self._correct_abnormal_route(
                            group,
                            legs,
                city,
                cache,
                planning_run_id,
                max_new_edges=correction_budget,
                        ),
                        timeout=remaining_seconds,
                    )
                except TimeoutError:
                    corrected_group, corrected_legs, new_calls = group, legs, 0
            api_calls += new_calls
            correction_budget -= new_calls
            corrected_groups.append((corrected_group, corrected_legs))

        warnings: list[str] = []
        if pending:
            warnings.append("部分相邻路线查询达到数据阶段超时，已使用直线距离估算并保留景点顺序。")
        if any(leg.is_fallback for _, legs in corrected_groups for leg in legs):
            warnings.append("部分路段使用了打车或直线距离降级方案，出发前请打开高德查看实时路线。")
        return corrected_groups, warnings, {"api_calls": api_calls, "fallbacks": fallback_count}

    async def _route_leg(
        self,
        origin: AttractionCandidate,
        destination: AttractionCandidate,
        city: str,
        planning_run_id: str,
    ) -> tuple[RouteLegEvidence, int]:
        straight_distance = haversine_km(origin.place, destination.place)
        primary_mode = (
            RouteMode.WALKING if straight_distance <= _WALKING_THRESHOLD_KM else RouteMode.TRANSIT
        )
        try:
            primary = await self._plan_route(
                origin,
                destination,
                primary_mode,
                city,
                planning_run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Amap primary route failed mode=%s exception_type=%s",
                primary_mode,
                type(exc).__name__,
            )
            return await self._driving_fallback(
                origin,
                destination,
                city,
                planning_run_id,
                calls=1,
            )

        if primary_mode == RouteMode.TRANSIT and self._transit_is_abnormal(primary):
            return await self._driving_fallback(
                origin,
                destination,
                city,
                planning_run_id,
                calls=1,
            )
        return _route_evidence(origin, destination, primary, is_fallback=False), 1

    async def _driving_fallback(
        self,
        origin: AttractionCandidate,
        destination: AttractionCandidate,
        city: str,
        planning_run_id: str,
        *,
        calls: int,
    ) -> tuple[RouteLegEvidence, int]:
        try:
            route = await self._plan_route(
                origin,
                destination,
                RouteMode.DRIVING,
                city,
                planning_run_id,
            )
            return _route_evidence(origin, destination, route, is_fallback=True), calls + 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Amap driving fallback failed exception_type=%s",
                type(exc).__name__,
            )
            return _estimated_leg(origin, destination), calls + 1

    async def _plan_route(
        self,
        origin: AttractionCandidate,
        destination: AttractionCandidate,
        mode: RouteMode,
        city: str,
        planning_run_id: str,
    ) -> RouteResult:
        assert self._client is not None
        started_at = monotonic()
        try:
            async with self._route_semaphore:
                result = await self._client.plan_route(
                    RoutePlanInput(
                        origin=_coordinate_input(origin),
                        destination=_coordinate_input(destination),
                        mode=mode,
                        city=city if mode == RouteMode.TRANSIT else None,
                    )
                )
        except BaseException:
            logger.info(
                "Amap route search failed planning_run_id=%s origin_poi_id=%s "
                "destination_poi_id=%s mode=%s route_search_latency_ms=%s",
                planning_run_id,
                origin.place.poi_id,
                destination.place.poi_id,
                mode,
                round((monotonic() - started_at) * 1_000),
            )
            raise
        logger.info(
            "Amap route search completed planning_run_id=%s origin_poi_id=%s "
            "destination_poi_id=%s mode=%s route_search_latency_ms=%s",
            planning_run_id,
            origin.place.poi_id,
            destination.place.poi_id,
            mode,
            round((monotonic() - started_at) * 1_000),
        )
        return result

    def _transit_is_abnormal(self, route: RouteResult) -> bool:
        return (
            (route.transfers or 0) > self._max_transit_transfers
            or route.duration_seconds > self._max_transit_duration_minutes * 60
            or (route.walking_distance_meters or 0) > self._max_walk_distance_meters
        )

    async def _correct_abnormal_route(
        self,
        group: list[AttractionCandidate],
        legs: list[RouteLegEvidence],
        city: str,
        cache: dict[tuple[str, str], RouteLegEvidence],
        planning_run_id: str,
        *,
        max_new_edges: int,
    ) -> tuple[list[AttractionCandidate], list[RouteLegEvidence], int]:
        if len(group) < 3 or not legs or max_new_edges <= 0:
            return group, legs, 0
        durations = [leg.duration_seconds or _LOCAL_ROUTE_ABNORMAL_SECONDS for leg in legs]
        median = statistics.median(durations)
        worst_index = max(range(len(legs)), key=lambda index: durations[index])
        worst = durations[worst_index]
        if worst < _LOCAL_ROUTE_ABNORMAL_SECONDS and worst < median * 2.5:
            return group, legs, 0

        candidate_group = list(group)
        candidate_group[worst_index], candidate_group[worst_index + 1] = (
            candidate_group[worst_index + 1],
            candidate_group[worst_index],
        )
        candidate_pairs = list(itertools.pairwise(candidate_group))
        missing = [
            pair
            for pair in candidate_pairs
            if (pair[0].place.poi_id, pair[1].place.poi_id) not in cache
        ]
        if len(missing) > max_new_edges:
            return group, legs, 0
        results = await asyncio.gather(
            *(
                self._route_leg(origin, destination, city, planning_run_id)
                for origin, destination in missing
            ),
            return_exceptions=True,
        )
        calls = 0
        for pair, result in zip(missing, results, strict=True):
            if isinstance(result, BaseException):
                cache[(pair[0].place.poi_id, pair[1].place.poi_id)] = _estimated_leg(*pair)
                continue
            leg, result_calls = result
            calls += result_calls
            cache[(pair[0].place.poi_id, pair[1].place.poi_id)] = leg
        candidate_legs = [
            cache[(origin.place.poi_id, destination.place.poi_id)]
            for origin, destination in candidate_pairs
        ]
        original_seconds = sum(leg.duration_seconds or 0 for leg in legs)
        candidate_seconds = sum(leg.duration_seconds or 0 for leg in candidate_legs)
        if candidate_seconds and candidate_seconds < original_seconds * 0.9:
            return candidate_group, candidate_legs, calls
        return group, legs, calls

    @staticmethod
    def _build_day(
        *,
        request: CityTripRequest,
        day_index: int,
        attractions: list[AttractionCandidate],
        legs: list[RouteLegEvidence],
    ) -> MapDayEvidence:
        assert request.start_date is not None
        estimated_transport_minutes = sum(
            max(1, round((leg.duration_seconds or 0) / 60)) for leg in legs
        )
        warnings = (
            ["当天存在降级路段，请在出发前使用地图导航复查。"]
            if any(leg.is_fallback for leg in legs)
            else []
        )
        return MapDayEvidence(
            day_index=day_index,
            date=request.start_date + timedelta(days=day_index - 1),
            attractions=[_place_evidence(item) for item in attractions],
            estimated_visit_minutes=sum(item.estimated_visit_minutes for item in attractions),
            estimated_transport_minutes=estimated_transport_minutes,
            route_legs=legs,
            warnings=warnings,
        )


def _place_evidence(candidate: AttractionCandidate) -> MapPlaceEvidence:
    keyword, rank = candidate.best_search
    place = candidate.place
    return MapPlaceEvidence(
        reference_id=f"poi_{place.poi_id}"[:100],
        poi_id=place.poi_id,
        name=place.name,
        address=place.address,
        poi_type=place.poi_type,
        location=place.location,
        adcode=place.adcode or None,
        city=place.city or None,
        search_query=keyword,
        search_rank=rank,
        estimated_visit_minutes=candidate.estimated_visit_minutes,
        matched_preferences=[str(item) for item in sorted(candidate.matched_preferences)],
        selection_reasons=candidate.selection_reasons,
        candidate_score=candidate.score,
    )


def _route_evidence(
    origin: AttractionCandidate,
    destination: AttractionCandidate,
    route: RouteResult,
    *,
    is_fallback: bool,
) -> RouteLegEvidence:
    return RouteLegEvidence(
        origin_ref=f"poi_{origin.place.poi_id}"[:100],
        destination_ref=f"poi_{destination.place.poi_id}"[:100],
        mode=route.mode,
        distance_meters=route.distance_meters,
        duration_seconds=route.duration_seconds,
        transfer_count=route.transfers,
        route_summary=route.route_summary,
        is_fallback=is_fallback,
    )


def _estimated_leg(
    origin: AttractionCandidate,
    destination: AttractionCandidate,
    *,
    timed_out: bool = False,
) -> RouteLegEvidence:
    distance_km = haversine_km(origin.place, destination.place)
    summary = (
        "路线查询超时，当前为直线距离预算估算；请在出发前打开高德确认。"
        if timed_out
        else "步行、公交和驾车路线均未获得可靠结果；当前为直线距离预算估算。"
    )
    return RouteLegEvidence(
        origin_ref=f"poi_{origin.place.poi_id}"[:100],
        destination_ref=f"poi_{destination.place.poi_id}"[:100],
        mode="estimated",
        distance_meters=round(distance_km * 1_000),
        duration_seconds=straight_line_transport_minutes(distance_km) * 60,
        route_summary=summary,
        is_fallback=True,
    )


def _coordinate_input(candidate: AttractionCandidate) -> AmapCoordinateInput:
    return AmapCoordinateInput(
        longitude=candidate.place.location.longitude,
        latitude=candidate.place.location.latitude,
    )


def _exclusion_reason(
    candidate: AttractionCandidate,
    selected: list[AttractionCandidate],
) -> str:
    if selected:
        nearest = min(haversine_km(candidate.place, item.place) for item in selected)
        if nearest > 25:
            return "距离主要景点区域较远且综合评分未进入容量上限"
    same_type = sum(item.attraction_type == candidate.attraction_type for item in selected)
    if same_type >= 2:
        return "为保持景点类型多样性未进入最终行程"
    return "综合评分未进入本次行程的景点数量与时长上限"


__all__ = [
    "MapPlanningClient",
    "MapTripCollectionError",
    "MapTripCollectionService",
]
