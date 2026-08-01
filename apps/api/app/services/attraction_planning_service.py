from __future__ import annotations

import itertools
import math
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

from app.schemas.amap import AmapPlace
from app.schemas.map_planning import MapDayEvidence, MapTripEvidence
from app.schemas.trip_planning import TripPreference, TripWeatherEvidence

ANCHOR_SEARCH_KEYWORDS = (
    "旅游景点",
    "风景名胜",
    "城市地标",
)

CATEGORY_SEARCH_KEYWORDS = (
    "历史古迹",
    "博物馆",
    "公园",
    "特色街区",
)

BASE_SEARCH_KEYWORDS = ANCHOR_SEARCH_KEYWORDS + CATEGORY_SEARCH_KEYWORDS

PREFERENCE_SEARCH_KEYWORDS: dict[TripPreference, tuple[str, ...]] = {
    TripPreference.HISTORY_CULTURE: ("历史古迹", "文化遗址", "名人故居", "古建筑"),
    TripPreference.MUSEUM_EXHIBITION: ("博物馆", "纪念馆", "美术馆", "展览馆"),
    TripPreference.NATURAL_SCENERY: ("风景区", "公园", "湿地", "湖泊"),
    TripPreference.CITY_LANDMARK: ("城市地标", "标志性建筑", "观景台"),
    TripPreference.CHARACTERISTIC_DISTRICT: ("历史街区", "步行街", "文化街区"),
    TripPreference.PHOTOGRAPHY: ("摄影打卡", "观景台", "城市地标"),
    TripPreference.FAMILY: ("亲子景点", "动物园", "科技馆", "海洋馆"),
    TripPreference.LEISURE: ("公园", "园林", "休闲街区"),
    TripPreference.NIGHT_VIEW: ("夜景", "夜游", "步行街"),
}

DAY_EFFECTIVE_BUDGET_MINUTES = 480
MAX_ATTRACTIONS_PER_DAY = 5
MIN_ATTRACTIONS_PER_DAY = 3
MAX_SELECTED_ATTRACTIONS = 25
FUZZY_DUPLICATE_DISTANCE_KM = 0.2

_FACILITY_NAME_MARKERS = (
    "停车场",
    "售票处",
    "售票厅",
    "出入口",
    "入口",
    "出口",
    "游客中心",
    "卫生间",
    "厕所",
    "便利店",
    "礼品店",
    "服务区",
    "公交站",
    "地铁站",
)
_FACILITY_TYPE_MARKERS = (
    "停车场",
    "公共厕所",
    "票务服务",
    "交通设施",
    "公交车站",
    "地铁站",
    "生活服务",
    "购物",
    "餐饮",
    "住宿",
    "汽车服务",
)
_NON_TOURISM_TYPE_MARKERS = (
    "公司企业",
    "产业园区",
    "商务住宅",
    "写字楼",
    "楼宇",
    "房产",
    "金融保险",
    "医疗保健",
    "政府机构",
)
_GENERIC_NAME_SUFFIXES = ("风景名胜区", "旅游景区", "风景区", "旅游区", "景区")
_LARGE_SCENIC_MARKERS = ("国家公园", "风景名胜区", "旅游度假区", "自然保护区")
_COMPOUND_NAME_SUFFIXES = ("广场", "博物院", "博物馆")
_REMOTE_LOW_CONFIDENCE_DISTANCE_KM = 20

_DURATION_MINUTES = {
    "large_scenic_area": 180,
    "large_museum": 150,
    "museum": 120,
    "park": 120,
    "historical_site": 90,
    "temple": 75,
    "former_residence": 60,
    "city_landmark": 60,
    "viewpoint": 45,
    "historic_district": 120,
    "commercial_pedestrian_area": 90,
    "family_attraction": 120,
    "other": 90,
}


@dataclass(frozen=True, slots=True)
class PoiSearchTask:
    keyword: str
    preference: TripPreference | None = None
    is_base: bool = False
    recall_kind: Literal["anchor", "category", "preference"] = "category"


@dataclass(slots=True)
class AttractionCandidate:
    place: AmapPlace
    normalized_name: str
    attraction_type: str
    estimated_visit_minutes: int
    search_ranks: dict[str, int] = field(default_factory=dict)
    search_kinds: dict[str, Literal["anchor", "category", "preference"]] = field(
        default_factory=dict
    )
    matched_preferences: set[TripPreference] = field(default_factory=set)
    base_hits: int = 0
    preference_hits: int = 0
    fame_score: float = 0.0
    fame_tier: Literal["S", "A", "B", "C"] = "C"
    preference_score: float = 0.0
    score: float = 0.0
    selection_reasons: list[str] = field(default_factory=list)

    @property
    def best_search(self) -> tuple[str, int]:
        return min(self.search_ranks.items(), key=lambda item: (item[1], item[0]))


@dataclass(slots=True)
class RejectedAttraction:
    place: AmapPlace
    reason: str
    search_ranks: dict[str, int] = field(default_factory=dict)


def build_poi_search_tasks(preferences: list[TripPreference]) -> list[PoiSearchTask]:
    tasks = [
        PoiSearchTask(
            keyword=keyword,
            is_base=True,
            recall_kind="anchor" if keyword in ANCHOR_SEARCH_KEYWORDS else "category",
        )
        for keyword in BASE_SEARCH_KEYWORDS
    ]
    seen = set(BASE_SEARCH_KEYWORDS)
    for preference in preferences:
        for keyword in PREFERENCE_SEARCH_KEYWORDS[preference]:
            if keyword in seen:
                # Execute an overlapping provider query once while retaining the preference hit.
                for index, task in enumerate(tasks):
                    if task.keyword == keyword and task.preference is None:
                        tasks[index] = PoiSearchTask(
                            keyword=task.keyword,
                            preference=preference,
                            is_base=task.is_base,
                            recall_kind=task.recall_kind,
                        )
                        break
                continue
            seen.add(keyword)
            tasks.append(
                PoiSearchTask(
                    keyword=keyword,
                    preference=preference,
                    is_base=False,
                    recall_kind="preference",
                )
            )
    return tasks


def normalize_poi_name(name: str, city: str) -> str:
    value = unicodedata.normalize("NFKC", name).strip()
    city_name = city.strip().removesuffix("市")
    value = re.sub(rf"^(?:{re.escape(city_name)}市?|{re.escape(city)})", "", value)
    value = "".join(
        character
        for character in value
        if not unicodedata.category(character).startswith(("P", "Z"))
    )
    for suffix in _GENERIC_NAME_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix) + 1:
            value = value[: -len(suffix)]
            break
    return value.casefold()


def attraction_rejection_reason(place: AmapPlace, city: str) -> str | None:
    if not place.poi_id or not place.name or (place.city and not _same_city(place.city, city)):
        return "POI 缺少必要信息或不属于目标城市"
    name = unicodedata.normalize("NFKC", place.name)
    poi_type = unicodedata.normalize("NFKC", place.poi_type)
    if any(marker in poi_type for marker in _FACILITY_TYPE_MARKERS):
        return "POI 类型属于服务设施，非游览景点"
    if any(marker in poi_type for marker in _NON_TOURISM_TYPE_MARKERS):
        return "POI 类型属于企业、园区或商务住宅，非游览景点"
    if any(marker in name for marker in _FACILITY_NAME_MARKERS):
        return "POI 名称属于服务设施，非游览景点"
    return None


def is_valid_attraction(place: AmapPlace, city: str) -> bool:
    return attraction_rejection_reason(place, city) is None


def merge_and_deduplicate_candidates(
    city: str,
    results: list[tuple[PoiSearchTask, list[AmapPlace]]],
) -> tuple[list[AttractionCandidate], dict[str, int], list[RejectedAttraction]]:
    exact: dict[str, AttractionCandidate] = {}
    rejected: dict[str, RejectedAttraction] = {}
    stats = {
        "raw": 0,
        "invalid": 0,
        "exact_duplicates": 0,
        "parent_duplicates": 0,
        "fuzzy_duplicates": 0,
    }
    for task, places in results:
        for rank, place in enumerate(places, start=1):
            stats["raw"] += 1
            rejection_reason = attraction_rejection_reason(place, city)
            if rejection_reason is not None:
                stats["invalid"] += 1
                rejected_item = rejected.get(place.poi_id)
                if rejected_item is None:
                    rejected_item = RejectedAttraction(place=place, reason=rejection_reason)
                    rejected[place.poi_id] = rejected_item
                rejected_item.search_ranks[task.keyword] = min(
                    rank,
                    rejected_item.search_ranks.get(task.keyword, rank),
                )
                continue
            candidate = exact.get(place.poi_id)
            if candidate is None:
                attraction_type = classify_attraction(place)
                candidate = AttractionCandidate(
                    place=place,
                    normalized_name=normalize_poi_name(place.name, city),
                    attraction_type=attraction_type,
                    estimated_visit_minutes=_DURATION_MINUTES[attraction_type],
                )
                exact[place.poi_id] = candidate
            else:
                stats["exact_duplicates"] += 1
            candidate.search_ranks[task.keyword] = min(
                rank,
                candidate.search_ranks.get(task.keyword, rank),
            )
            candidate.search_kinds[task.keyword] = task.recall_kind
            candidate.base_hits += int(task.is_base)
            candidate.preference_hits += int(task.preference is not None)
            if task.preference is not None:
                candidate.matched_preferences.add(task.preference)

    all_exact = dict(exact)
    for child in list(all_exact.values()):
        target = child
        seen = {child.place.poi_id}
        while (
            target.place.parent_poi_id
            and target.place.parent_poi_id not in seen
            and target.place.parent_poi_id in all_exact
        ):
            seen.add(target.place.parent_poi_id)
            target = all_exact[target.place.parent_poi_id]
        if target is child:
            continue
        _merge_candidate_evidence(target, child)
        exact.pop(child.place.poi_id, None)
        stats["parent_duplicates"] += 1

    ordered = sorted(exact.values(), key=_pre_score_key)
    kept: list[AttractionCandidate] = []
    for candidate in ordered:
        duplicate = next(
            (existing for existing in kept if _is_fuzzy_duplicate(existing, candidate)),
            None,
        )
        if duplicate is None:
            kept.append(candidate)
            continue
        stats["fuzzy_duplicates"] += 1
        _merge_candidate_evidence(duplicate, candidate)
    return kept, stats, list(rejected.values())


def score_candidates(candidates: list[AttractionCandidate]) -> None:
    for candidate in candidates:
        search_kinds = {
            keyword: candidate.search_kinds.get(
                keyword,
                "anchor" if keyword in ANCHOR_SEARCH_KEYWORDS else "category",
            )
            for keyword in candidate.search_ranks
        }
        provider_ranks = [
            rank
            for keyword, rank in candidate.search_ranks.items()
            if search_kinds[keyword] != "preference"
        ]
        anchor_ranks = [
            rank
            for keyword, rank in candidate.search_ranks.items()
            if search_kinds[keyword] == "anchor"
        ]
        category_ranks = [
            rank
            for keyword, rank in candidate.search_ranks.items()
            if search_kinds[keyword] == "category"
        ]
        best_anchor_score = max(0.0, (11 - min(anchor_ranks)) / 10) * 30 if anchor_ranks else 0.0
        anchor_coverage_score = min(len(anchor_ranks), 3) / 3 * 20
        cross_query_score = min(len(provider_ranks), 4) / 4 * 15
        category_rank_score = (
            max(0.0, (11 - min(category_ranks)) / 10) * 10 if category_ranks else 0.0
        )
        rating_score = (
            7.5
            if candidate.place.rating is None
            else max(0.0, min(1.0, (candidate.place.rating - 3.0) / 2.0)) * 15
        )
        hierarchy_score = 2.0 if candidate.place.parent_poi_id else 10.0
        candidate.fame_score = round(
            best_anchor_score
            + anchor_coverage_score
            + cross_query_score
            + category_rank_score
            + rating_score
            + hierarchy_score,
            3,
        )
        candidate.preference_score = round(
            min(
                100.0,
                len(candidate.matched_preferences) * 35
                + min(candidate.preference_hits, 3) * 10
                + (10 if candidate.base_hits and candidate.preference_hits else 0),
            ),
            3,
        )
        best_rank = min(candidate.search_ranks.values(), default=10)
        retrieval_quality = min(
            100.0,
            min(len(candidate.search_ranks), 5) / 5 * 50 + max(0.0, (11 - best_rank) / 10) * 50,
        )

        distances = sorted(
            haversine_km(candidate.place, other.place)
            for other in candidates
            if other.place.poi_id != candidate.place.poi_id
        )
        nearest = distances[0] if distances else 0.0
        spatial_score = 4.0 if nearest <= 8 else 2.0 if nearest <= 20 else 0.0
        isolation_penalty = 10.0 if nearest > 25 and candidate.fame_score < 45 else 0.0
        candidate.score = round(
            candidate.fame_score * 0.65
            + candidate.preference_score * 0.25
            + retrieval_quality * 0.1
            + spatial_score
            - isolation_penalty,
            3,
        )

    for candidate in candidates:
        candidate.fame_tier = (
            "S"
            if candidate.fame_score >= 65
            else "A"
            if candidate.fame_score >= 45
            else "B"
            if candidate.fame_score >= 25
            else "C"
        )
    if candidates and not any(item.fame_tier in {"S", "A"} for item in candidates):
        max(
            candidates,
            key=lambda item: (item.fame_score, item.score, item.place.poi_id),
        ).fame_tier = "A"

    for candidate in candidates:
        reasons: list[str] = []
        if candidate.fame_tier in {"S", "A"}:
            reasons.append(f"高德通用景点检索知名度为{candidate.fame_tier}级")
        if len(candidate.search_ranks) >= 2:
            reasons.append("多组关键词检索结果中稳定出现")
        if min(candidate.search_ranks.values(), default=99) <= 5:
            reasons.append("高德关键词检索排名靠前")
        if candidate.place.rating is not None and candidate.place.rating >= 4.5:
            reasons.append(f"高德评分较高（{candidate.place.rating:g}分）")
        if candidate.matched_preferences:
            reasons.append("与用户的标准偏好标签匹配")
        candidate.selection_reasons = reasons or ["来自目标城市内的有效高德景点结果"]


def exclude_remote_low_confidence_candidates(
    candidates: list[AttractionCandidate],
) -> tuple[list[AttractionCandidate], list[RejectedAttraction]]:
    trusted_cores = [item for item in candidates if item.fame_tier in {"S", "A"}]
    if not trusted_cores:
        return candidates, []
    kept: list[AttractionCandidate] = []
    rejected: list[RejectedAttraction] = []
    for candidate in candidates:
        if candidate.fame_tier == "S":
            kept.append(candidate)
            continue
        other_trusted_cores = [
            item for item in trusted_cores if item.place.poi_id != candidate.place.poi_id
        ]
        if not other_trusted_cores:
            kept.append(candidate)
            continue
        nearest_core_distance = min(
            haversine_km(candidate.place, item.place) for item in other_trusted_cores
        )
        if (
            candidate.fame_tier != "S"
            and nearest_core_distance > _REMOTE_LOW_CONFIDENCE_DISTANCE_KM
        ):
            rejected.append(
                RejectedAttraction(
                    place=candidate.place,
                    reason=(
                        "候选景点距主旅游核心超过 "
                        f"{_REMOTE_LOW_CONFIDENCE_DISTANCE_KM} 公里"
                    ),
                    search_ranks=dict(candidate.search_ranks),
                )
            )
            continue
        kept.append(candidate)
    return kept, rejected


def match_candidate_preferences(
    candidates: list[AttractionCandidate],
    preferences: list[TripPreference],
) -> None:
    markers: dict[TripPreference, tuple[str, ...]] = {
        TripPreference.HISTORY_CULTURE: ("历史", "文化", "遗址", "古迹", "故居", "古建筑"),
        TripPreference.MUSEUM_EXHIBITION: ("博物馆", "博物院", "纪念馆", "美术馆", "展览馆"),
        TripPreference.NATURAL_SCENERY: ("风景", "公园", "湿地", "湖", "山", "园林"),
        TripPreference.CITY_LANDMARK: ("地标", "广场", "塔", "大桥", "观景台"),
        TripPreference.CHARACTERISTIC_DISTRICT: ("街区", "古街", "古镇", "步行街"),
        TripPreference.PHOTOGRAPHY: ("观景", "地标", "风景", "摄影"),
        TripPreference.FAMILY: ("亲子", "儿童", "动物园", "海洋馆", "科技馆", "游乐园"),
        TripPreference.LEISURE: ("休闲", "公园", "园林", "街区"),
        TripPreference.NIGHT_VIEW: ("夜景", "夜游", "步行街"),
    }
    for candidate in candidates:
        text = f"{candidate.place.name};{candidate.place.poi_type}"
        candidate.matched_preferences.update(
            preference
            for preference in preferences
            if any(marker in text for marker in markers[preference])
        )


def select_diverse_candidates(
    candidates: list[AttractionCandidate],
    days: int,
) -> tuple[list[AttractionCandidate], list[AttractionCandidate]]:
    if not candidates:
        return [], []
    target = min(days * 4, MAX_SELECTED_ATTRACTIONS, len(candidates))
    minimum = min(days * MIN_ATTRACTIONS_PER_DAY, len(candidates))
    selected = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.fame_tier in {"S", "A"} and not candidate.place.parent_poi_id
        ),
        key=lambda item: (-item.fame_score, -item.score, item.place.poi_id),
    )[:days]
    remaining = [candidate for candidate in candidates if candidate not in selected]
    while remaining and len(selected) < target:

        def mmr(candidate: AttractionCandidate) -> tuple[float, float, str]:
            name_similarity = max(
                (
                    SequenceMatcher(
                        None,
                        candidate.normalized_name,
                        item.normalized_name,
                    ).ratio()
                    for item in selected
                ),
                default=0.0,
            )
            similarity_penalty = max(0.0, name_similarity - 0.65) * 20
            long_visit_penalty = (
                3.0
                if candidate.estimated_visit_minutes >= 150
                and sum(item.estimated_visit_minutes >= 150 for item in selected) >= days
                else 0.0
            )
            return (
                candidate.score - similarity_penalty - long_visit_penalty,
                candidate.score,
                candidate.place.poi_id,
            )

        chosen = max(remaining, key=mmr)
        selected.append(chosen)
        remaining.remove(chosen)

    # Do not reduce below the documented minimum merely because the duration mix is long.
    # Reserve roughly two hours per day for movement between three to five attractions.
    while (
        len(selected) > minimum
        and sum(item.estimated_visit_minutes for item in selected) > days * 360
    ):
        selected.pop()
    return selected, [item for item in candidates if item not in selected]


class DailyClusterPlanner:
    def __init__(self, *, max_iterations: int = 20) -> None:
        self._max_iterations = max_iterations

    def plan(
        self,
        candidates: list[AttractionCandidate],
        days: int,
    ) -> list[list[AttractionCandidate]]:
        if not candidates:
            return [[] for _ in range(days)]
        seed_count = min(days, len(candidates))
        seeds = self._select_seeds(candidates, seed_count)
        groups = [[seed] for seed in seeds]
        groups.extend([] for _ in range(days - len(groups)))
        remaining = [item for item in candidates if item not in seeds]
        balanced_capacity = min(
            MAX_ATTRACTIONS_PER_DAY,
            math.ceil(len(candidates) / days),
        )

        # Fill days round-robin so a sparse result degrades evenly instead of starving later days.
        while remaining:
            underfilled = [
                group for group in groups if group and len(group) < MIN_ATTRACTIONS_PER_DAY
            ]
            if not underfilled:
                break
            for group in underfilled:
                if not remaining:
                    break
                chosen = min(
                    remaining,
                    key=lambda item: (
                        self._assignment_cost(item, group),
                        -item.score,
                        item.place.poi_id,
                    ),
                )
                group.append(chosen)
                remaining.remove(chosen)

        for candidate in sorted(remaining, key=lambda item: (-item.score, item.place.poi_id)):
            ranked_groups = sorted(
                range(len(groups)),
                key=lambda index: (
                    self._assignment_cost(candidate, groups[index]),
                    len(groups[index]),
                    index,
                ),
            )
            target_index = next(
                (
                    index
                    for index in ranked_groups
                    if len(groups[index]) < balanced_capacity
                    and _group_budget_minutes([*groups[index], candidate])
                    <= DAY_EFFECTIVE_BUDGET_MINUTES
                ),
                None,
            )
            if target_index is None:
                target_index = next(
                    (
                        index
                        for index in ranked_groups
                        if len(groups[index]) < MAX_ATTRACTIONS_PER_DAY
                    ),
                    ranked_groups[0],
                )
            groups[target_index].append(candidate)

        self._improve_by_swaps(groups)
        return [optimize_daily_route(group) for group in groups]

    def _select_seeds(
        self,
        candidates: list[AttractionCandidate],
        count: int,
    ) -> list[AttractionCandidate]:
        top_level = [item for item in candidates if not item.place.parent_poi_id]
        prominent = [item for item in top_level if item.fame_tier in {"S", "A"}]
        if len(prominent) >= count:
            pool = prominent
        else:
            pool = [*prominent, *(item for item in top_level if item.fame_tier == "B")]
            if len(pool) < count:
                pool.extend(item for item in top_level if item.fame_tier == "C")
            if len(pool) < count:
                pool.extend(item for item in candidates if item not in pool)

        first = max(
            pool,
            key=lambda item: (item.fame_score, item.score, item.place.poi_id),
        )
        seeds = [first]
        local_masses = {item.place.poi_id: self._local_fame_mass(item, candidates) for item in pool}
        max_local_mass = max(1.0, max(local_masses.values(), default=0.0))
        while len(seeds) < count:
            choices = [item for item in pool if item not in seeds]
            chosen = max(
                choices,
                key=lambda item: (
                    item.fame_score * 0.5
                    + min(
                        min(haversine_km(item.place, seed.place) for seed in seeds),
                        12.0,
                    )
                    / 12.0
                    * (local_masses[item.place.poi_id] / max_local_mass * 25 + 25),
                    item.score,
                    item.place.poi_id,
                ),
            )
            seeds.append(chosen)
        return seeds

    @staticmethod
    def _local_fame_mass(
        candidate: AttractionCandidate,
        candidates: list[AttractionCandidate],
    ) -> float:
        return candidate.fame_score + sum(
            other.fame_score * max(0.0, 1 - haversine_km(candidate.place, other.place) / 8)
            for other in candidates
            if other is not candidate
        )

    @staticmethod
    def _assignment_cost(
        candidate: AttractionCandidate,
        group: list[AttractionCandidate],
    ) -> float:
        if not group:
            return 0.0
        return min(haversine_km(candidate.place, item.place) for item in group)

    def _improve_by_swaps(self, groups: list[list[AttractionCandidate]]) -> None:
        distance_cache: dict[tuple[str, str], float] = {}
        cost_cache: dict[tuple[str, ...], float] = {}

        def distance(left: AttractionCandidate, right: AttractionCandidate) -> float:
            key = tuple(sorted((left.place.poi_id, right.place.poi_id)))
            if key not in distance_cache:
                distance_cache[key] = haversine_km(left.place, right.place)
            return distance_cache[key]

        def cost(group: list[AttractionCandidate]) -> float:
            key = tuple(sorted(item.place.poi_id for item in group))
            if key not in cost_cache:
                pair_distances = sorted(
                    distance(left, right) for left, right in itertools.combinations(group, 2)
                )
                compactness = sum(pair_distances) / max(1, len(group) - 1)
                visit_minutes = sum(item.estimated_visit_minutes for item in group)
                transport_minutes = sum(
                    straight_line_transport_minutes(item)
                    for item in pair_distances[: max(0, len(group) - 1)]
                )
                over_budget = max(
                    0,
                    visit_minutes + transport_minutes - DAY_EFFECTIVE_BUDGET_MINUTES,
                )
                cost_cache[key] = compactness + over_budget * 0.2
            return cost_cache[key]

        def total_cost(current_groups: list[list[AttractionCandidate]]) -> float:
            visit_totals = [
                sum(item.estimated_visit_minutes for item in group) for group in current_groups
            ]
            imbalance = statistics.pvariance(visit_totals) / 1_000 if visit_totals else 0.0
            return sum(cost(group) for group in current_groups) + imbalance

        for _ in range(self._max_iterations):
            current_cost = total_cost(groups)
            best: tuple[float, int, int, int, int] | None = None
            for left_index, right_index in itertools.combinations(range(len(groups)), 2):
                left = groups[left_index]
                right = groups[right_index]
                preserve_prominent_anchors = all(
                    any(item.fame_tier in {"S", "A"} for item in group) for group in (left, right)
                )
                for left_item, right_item in itertools.product(range(len(left)), range(len(right))):
                    new_left = list(left)
                    new_right = list(right)
                    new_left[left_item], new_right[right_item] = (
                        new_right[right_item],
                        new_left[left_item],
                    )
                    if (
                        max(
                            sum(item.estimated_visit_minutes for item in new_left),
                            sum(item.estimated_visit_minutes for item in new_right),
                        )
                        > DAY_EFFECTIVE_BUDGET_MINUTES
                    ):
                        continue
                    if preserve_prominent_anchors and not all(
                        any(item.fame_tier in {"S", "A"} for item in group)
                        for group in (new_left, new_right)
                    ):
                        continue
                    candidate_groups = list(groups)
                    candidate_groups[left_index] = new_left
                    candidate_groups[right_index] = new_right
                    improvement = current_cost - total_cost(candidate_groups)
                    candidate = (
                        improvement,
                        left_index,
                        right_index,
                        left_item,
                        right_item,
                    )
                    if improvement > 0.01 and (best is None or candidate > best):
                        best = candidate
            if best is None:
                return
            _, left_index, right_index, left_item, right_item = best
            groups[left_index][left_item], groups[right_index][right_item] = (
                groups[right_index][right_item],
                groups[left_index][left_item],
            )
            if total_cost(groups) >= current_cost:
                return


def optimize_daily_route(group: list[AttractionCandidate]) -> list[AttractionCandidate]:
    if len(group) < 2:
        return list(group)
    return list(
        min(
            itertools.permutations(group),
            key=lambda route: (_route_cost(route), tuple(item.place.poi_id for item in route)),
        )
    )


def match_weather_to_days(
    evidence: MapTripEvidence,
    weather: TripWeatherEvidence,
) -> MapTripEvidence:
    if len(evidence.days) < 2 or not any(day.coverage == "available" for day in weather.days):
        return evidence
    weather_by_date = {day.date: day for day in weather.days}
    dated_slots = [day for day in evidence.days if day.date in weather_by_date]
    if len(dated_slots) < 2:
        return evidence
    best = min(
        itertools.permutations(evidence.days),
        key=lambda ordering: sum(
            _weather_mismatch(day, weather_by_date[slot.date])
            for day, slot in zip(ordering, evidence.days, strict=True)
        ),
    )
    remapped = [
        day.model_copy(update={"day_index": index, "date": slot.date})
        for index, (day, slot) in enumerate(zip(best, evidence.days, strict=True), start=1)
    ]
    return evidence.model_copy(update={"days": remapped})


def haversine_km(left: AmapPlace, right: AmapPlace) -> float:
    left_lat = math.radians(left.location.latitude)
    right_lat = math.radians(right.location.latitude)
    delta_lat = right_lat - left_lat
    delta_lon = math.radians(right.location.longitude - left.location.longitude)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(left_lat) * math.cos(right_lat) * math.sin(delta_lon / 2) ** 2
    )
    return 6371.0088 * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def classify_attraction(place: AmapPlace) -> str:
    text = f"{place.name};{place.poi_type}"
    if any(marker in text for marker in _LARGE_SCENIC_MARKERS):
        return "large_scenic_area"
    if "博物院" in text or "省博物馆" in text:
        return "large_museum"
    if any(marker in text for marker in ("博物馆", "纪念馆", "美术馆", "展览馆", "科技馆")):
        return "museum"
    if "故居" in text:
        return "former_residence"
    if any(marker in text for marker in ("寺", "庙", "道观", "教堂")):
        return "temple"
    if any(marker in text for marker in ("历史遗址", "文物古迹", "古建筑", "陵园", "遗址")):
        return "historical_site"
    if any(marker in text for marker in ("步行街", "商业街")):
        return "commercial_pedestrian_area"
    if any(marker in text for marker in ("历史街区", "古镇", "古街", "文化街区")):
        return "historic_district"
    if any(marker in text for marker in ("公园", "园林", "湿地", "植物园")):
        return "park"
    if any(marker in text for marker in ("观景台", "观景点")):
        return "viewpoint"
    if any(marker in text for marker in ("动物园", "海洋馆", "游乐园", "主题乐园")):
        return "family_attraction"
    if any(marker in text for marker in ("地标", "广场", "塔", "大桥")):
        return "city_landmark"
    return "other"


def straight_line_transport_minutes(distance_km: float) -> int:
    if distance_km <= 1:
        return 15
    if distance_km <= 3:
        return 25
    if distance_km <= 8:
        return 40
    return 60


def _same_city(left: str, right: str) -> bool:
    normalized_left = left.strip().removesuffix("市").casefold()
    normalized_right = right.strip().removesuffix("市").casefold()
    return (
        normalized_left == normalized_right
        or normalized_left in normalized_right
        or normalized_right in normalized_left
    )


def _pre_score_key(candidate: AttractionCandidate) -> tuple[int, int, int, str]:
    return (
        -len(candidate.search_ranks),
        min(candidate.search_ranks.values(), default=99),
        -candidate.base_hits,
        candidate.place.poi_id,
    )


def _is_fuzzy_duplicate(left: AttractionCandidate, right: AttractionCandidate) -> bool:
    if "large_scenic_area" in {left.attraction_type, right.attraction_type}:
        return False
    if _is_nearby_name_containment(left, right):
        return True
    if left.attraction_type != right.attraction_type:
        return False
    similarity = SequenceMatcher(None, left.normalized_name, right.normalized_name).ratio()
    return (
        similarity >= 0.88 and haversine_km(left.place, right.place) < FUZZY_DUPLICATE_DISTANCE_KM
    )


def _is_nearby_name_containment(
    left: AttractionCandidate,
    right: AttractionCandidate,
) -> bool:
    shorter, longer = sorted(
        (left.normalized_name, right.normalized_name),
        key=lambda item: (len(item), item),
    )
    return (
        len(shorter) >= 3
        and longer.removeprefix(shorter) in _COMPOUND_NAME_SUFFIXES
        and haversine_km(left.place, right.place) < FUZZY_DUPLICATE_DISTANCE_KM
    )


def _merge_candidate_evidence(
    target: AttractionCandidate,
    duplicate: AttractionCandidate,
) -> None:
    for keyword, rank in duplicate.search_ranks.items():
        target.search_ranks[keyword] = min(rank, target.search_ranks.get(keyword, rank))
    target.search_kinds.update(duplicate.search_kinds)
    target.matched_preferences.update(duplicate.matched_preferences)
    target.base_hits += duplicate.base_hits
    target.preference_hits += duplicate.preference_hits


def _group_budget_minutes(group: list[AttractionCandidate]) -> int:
    ordered = optimize_daily_route(group)
    visit = sum(item.estimated_visit_minutes for item in ordered)
    transport = sum(
        straight_line_transport_minutes(haversine_km(left.place, right.place))
        for left, right in itertools.pairwise(ordered)
    )
    return visit + transport


def _route_cost(route: tuple[AttractionCandidate, ...]) -> float:
    distance = sum(
        haversine_km(left.place, right.place) for left, right in itertools.pairwise(route)
    )
    penalty = 0.0
    last_index = len(route) - 1
    for index, item in enumerate(route):
        if item.estimated_visit_minutes >= 150 and index == last_index:
            penalty += 8
        if item.attraction_type in {"historic_district", "commercial_pedestrian_area"}:
            penalty += max(0, (len(route) // 2) - index) * 2
    for first, middle, last in zip(route, route[1:], route[2:], strict=False):
        direct = haversine_km(first.place, last.place)
        detour = haversine_km(first.place, middle.place) + haversine_km(
            middle.place,
            last.place,
        )
        penalty += max(0.0, detour - direct * 1.8)
    return distance + penalty


def _weather_mismatch(day: MapDayEvidence, weather: object) -> float:
    day_weather = str(getattr(weather, "day_weather", "") or "")
    temperature_text = str(getattr(weather, "day_temperature", "") or "")
    attraction_types = [place.poi_type for place in day.attractions]
    indoor = sum(
        any(marker in item for marker in ("博物馆", "纪念馆", "美术馆", "展览馆", "科技馆"))
        for item in attraction_types
    )
    outdoor = sum(
        any(marker in item for marker in ("公园", "风景", "湿地", "湖", "山", "街区"))
        for item in attraction_types
    )
    score = 0.0
    if any(marker in day_weather for marker in ("雨", "雪", "雷")):
        score += outdoor * 5 - indoor * 3
    elif any(marker in day_weather for marker in ("晴", "多云")):
        score += indoor - outdoor * 2
    try:
        if float(temperature_text) >= 33:
            walking_km = sum(
                (leg.distance_meters or 0) / 1000 for leg in day.route_legs if leg.mode == "walking"
            )
            score += walking_km * 2 + outdoor
    except ValueError:
        pass
    return score


__all__ = [
    "AttractionCandidate",
    "RejectedAttraction",
    "BASE_SEARCH_KEYWORDS",
    "DAY_EFFECTIVE_BUDGET_MINUTES",
    "DailyClusterPlanner",
    "PoiSearchTask",
    "build_poi_search_tasks",
    "classify_attraction",
    "attraction_rejection_reason",
    "exclude_remote_low_confidence_candidates",
    "haversine_km",
    "is_valid_attraction",
    "match_weather_to_days",
    "match_candidate_preferences",
    "merge_and_deduplicate_candidates",
    "normalize_poi_name",
    "optimize_daily_route",
    "score_candidates",
    "select_diverse_candidates",
    "straight_line_transport_minutes",
]
