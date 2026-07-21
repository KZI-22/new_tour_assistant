from __future__ import annotations

from datetime import date
from time import perf_counter

from app.schemas.amap import AmapCoordinate, AmapPlace
from app.schemas.map_planning import MapDayEvidence, MapPlaceEvidence, MapTripEvidence
from app.schemas.trip_planning import (
    CityTripRequest,
    DailyWeatherEvidence,
    TripPreference,
    TripWeatherEvidence,
)
from app.services.attraction_planning_service import (
    AttractionCandidate,
    DailyClusterPlanner,
    PoiSearchTask,
    build_poi_search_tasks,
    classify_attraction,
    match_weather_to_days,
    merge_and_deduplicate_candidates,
    normalize_poi_name,
    optimize_daily_route,
    score_candidates,
    select_diverse_candidates,
)


def place(
    poi_id: str,
    name: str,
    longitude: float,
    *,
    poi_type: str = "风景名胜",
) -> AmapPlace:
    return AmapPlace(
        poi_id=poi_id,
        name=name,
        address=f"{name}地址",
        province="江苏省",
        city="南京市",
        district="玄武区",
        adcode="320102",
        poi_type=poi_type,
        location=AmapCoordinate(longitude=longitude, latitude=32.05),
    )


def candidate(
    poi_id: str,
    longitude: float,
    *,
    score: float,
    poi_type: str = "风景名胜",
) -> AttractionCandidate:
    current = place(poi_id, f"景点{poi_id}", longitude, poi_type=poi_type)
    attraction_type = classify_attraction(current)
    return AttractionCandidate(
        place=current,
        normalized_name=normalize_poi_name(current.name, "南京"),
        attraction_type=attraction_type,
        estimated_visit_minutes=90,
        search_ranks={"景点": 1},
        base_hits=1,
        score=score,
    )


def evidence_place(
    poi_id: str,
    poi_type: str,
    longitude: float,
) -> MapPlaceEvidence:
    return MapPlaceEvidence(
        reference_id=f"poi_{poi_id}",
        poi_id=poi_id,
        name=f"景点{poi_id}",
        address="地址",
        poi_type=poi_type,
        location=AmapCoordinate(longitude=longitude, latitude=32.05),
        search_query="景点",
        search_rank=1,
        estimated_visit_minutes=90,
        candidate_score=10,
    )


def test_request_maps_free_text_to_supported_preference_tags() -> None:
    request = CityTripRequest(
        destination_city="南京",
        duration_days=2,
        start_date=date(2026, 8, 10),
        interests=["喜欢历史人文", "逛博物馆", "夜游看夜景", "未知偏好"],
    )

    assert request.interests == [
        TripPreference.HISTORY_CULTURE,
        TripPreference.MUSEUM_EXHIBITION,
        TripPreference.NIGHT_VIEW,
    ]


def test_search_tasks_always_include_base_queries_and_preference_expansions() -> None:
    tasks = build_poi_search_tasks(
        [TripPreference.HISTORY_CULTURE, TripPreference.MUSEUM_EXHIBITION]
    )
    keywords = [task.keyword for task in tasks]

    assert {"风景名胜", "历史古迹", "博物馆", "公园", "城市地标", "特色街区"} <= set(
        keywords
    )
    assert {"文化遗址", "名人故居", "古建筑", "纪念馆", "美术馆", "展览馆"} <= set(
        keywords
    )
    assert len(keywords) == len(set(keywords))


def test_normalization_exact_and_fuzzy_dedup_preserve_recall_evidence() -> None:
    first = place("park-1", "南京人民公园景区", 118.8000, poi_type="风景名胜;公园广场")
    fuzzy = place("park-2", "人民公园", 118.8005, poi_type="风景名胜;公园广场")
    parking = place("parking", "人民公园停车场", 118.8003, poi_type="交通设施;停车场")
    task_a = PoiSearchTask(keyword="公园", is_base=True)
    task_b = PoiSearchTask(
        keyword="休闲街区",
        preference=TripPreference.LEISURE,
    )

    candidates, stats = merge_and_deduplicate_candidates(
        "南京",
        [(task_a, [first, parking]), (task_b, [fuzzy, first])],
    )

    assert normalize_poi_name(first.name, "南京") == "人民公园"
    assert len(candidates) == 1
    assert set(candidates[0].search_ranks) == {"公园", "休闲街区"}
    assert candidates[0].matched_preferences == {TripPreference.LEISURE}
    assert stats == {"raw": 4, "invalid": 1, "exact_duplicates": 1, "fuzzy_duplicates": 1}


def test_large_scenic_area_sub_pois_are_not_spatially_merged() -> None:
    first = place(
        "scenic-1",
        "国家公园东区",
        118.8000,
        poi_type="风景名胜;国家公园",
    )
    second = place(
        "scenic-2",
        "国家公园东区景区",
        118.8003,
        poi_type="风景名胜;国家公园",
    )

    candidates, stats = merge_and_deduplicate_candidates(
        "南京",
        [(PoiSearchTask(keyword="风景名胜", is_base=True), [first, second])],
    )

    assert len(candidates) == 2
    assert stats["fuzzy_duplicates"] == 0


def test_scoring_and_mmr_selection_are_deterministic_and_diverse() -> None:
    candidates = [
        candidate("museum-1", 118.80, score=0, poi_type="科教文化服务;博物馆"),
        candidate("museum-2", 118.81, score=0, poi_type="科教文化服务;博物馆"),
        candidate("park", 118.82, score=0, poi_type="风景名胜;公园广场"),
        candidate("street", 118.83, score=0, poi_type="风景名胜;特色街区"),
    ]
    for index, item in enumerate(candidates):
        item.search_ranks = {"景点": index + 1, "城市地标": index + 2}
        item.base_hits = 2
    score_candidates(candidates)

    selected, excluded = select_diverse_candidates(candidates, 1)

    assert len(selected) == 4
    assert not excluded
    assert selected == select_diverse_candidates(candidates, 1)[0]
    assert len({item.attraction_type for item in selected[:3]}) >= 2


def test_spread_seeds_and_capacity_assignment_keep_nearby_places_together() -> None:
    candidates = [
        candidate("west-1", 118.70, score=100),
        candidate("west-2", 118.702, score=90),
        candidate("west-3", 118.704, score=80),
        candidate("east-1", 118.90, score=95),
        candidate("east-2", 118.902, score=85),
        candidate("east-3", 118.904, score=75),
    ]

    groups = DailyClusterPlanner(max_iterations=20).plan(candidates, 2)

    assert [len(group) for group in groups] == [3, 3]
    assert all(
        max(item.place.location.longitude for item in group)
        - min(item.place.location.longitude for item in group)
        < 0.01
        for group in groups
    )


def test_daily_route_optimizer_enumerates_open_path_order() -> None:
    route = optimize_daily_route(
        [
            candidate("middle", 118.81, score=90),
            candidate("right", 118.82, score=80),
            candidate("left", 118.80, score=100),
        ]
    )
    longitudes = [item.place.location.longitude for item in route]

    assert longitudes in [sorted(longitudes), sorted(longitudes, reverse=True)]


def test_weather_matcher_only_swaps_route_groups_between_dates() -> None:
    outdoor = MapDayEvidence(
        day_index=1,
        date=date(2026, 8, 10),
        attractions=[evidence_place("park", "风景名胜;公园", 118.80)],
        estimated_visit_minutes=90,
    )
    indoor = MapDayEvidence(
        day_index=2,
        date=date(2026, 8, 11),
        attractions=[evidence_place("museum", "科教文化服务;博物馆", 118.81)],
        estimated_visit_minutes=90,
    )
    evidence = MapTripEvidence(
        city="南京",
        planning_run_id="weather-match",
        days=[outdoor, indoor],
    )
    weather = TripWeatherEvidence(
        city="南京",
        days=[
            DailyWeatherEvidence(
                date=date(2026, 8, 10),
                coverage="available",
                day_weather="大雨",
                day_temperature="29",
            ),
            DailyWeatherEvidence(
                date=date(2026, 8, 11),
                coverage="available",
                day_weather="晴",
                day_temperature="30",
            ),
        ],
    )

    matched = match_weather_to_days(evidence, weather)

    assert matched.days[0].attractions[0].poi_id == "museum"
    assert matched.days[1].attractions[0].poi_id == "park"
    assert [day.date for day in matched.days] == [date(2026, 8, 10), date(2026, 8, 11)]


def test_local_planning_algorithms_complete_under_one_hundred_milliseconds() -> None:
    candidates = [
        candidate(
            f"poi-{cluster}-{index}",
            118.0 + cluster * 0.2 + index * 0.002,
            score=100 - cluster * 5 - index,
        )
        for cluster in range(5)
        for index in range(5)
    ]

    started_at = perf_counter()
    selected, _ = select_diverse_candidates(candidates, 5)
    groups = DailyClusterPlanner(max_iterations=20).plan(selected, 5)
    elapsed = perf_counter() - started_at

    assert len(groups) == 5
    assert elapsed < 0.1
