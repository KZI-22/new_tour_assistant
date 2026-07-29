from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from app.graphs.trip_planner_nodes import (
    BuildItinerarySkeletonNode,
    EvidenceJoinNode,
)
from app.schemas.amap import AmapCoordinate
from app.schemas.map_planning import (
    MapDayEvidence,
    MapDayNarrative,
    MapPlaceEvidence,
    MapPlaceNarrative,
    MapTripEvidence,
    RouteLegEvidence,
)
from app.schemas.trip_capabilities import (
    CapabilityPlan,
    HotelCapabilityPlan,
    TransportCapabilityPlan,
    TripPlanningRequest,
)
from app.schemas.trip_evidence import (
    EvidenceStatus,
    MapWeatherEvidenceBundle,
    RawCapabilityEvidence,
)
from app.schemas.trip_itinerary import TripNarrativePlan
from app.schemas.trip_planning import (
    CityTripRequest,
    DailyWeatherEvidence,
    TripWeatherEvidence,
)
from app.services.trip_evidence_joiner import join_trip_evidence
from app.services.trip_itinerary_generator import (
    TripItineraryGenerator,
    build_trip_generation_prompt,
    build_trip_narrative_skeleton,
)
from app.services.trip_itinerary_renderer import (
    render_trip_itinerary,
    split_trip_itinerary_sections,
)
from app.services.trip_plan_validator import TripPlanValidator
from app.services.weather_advice_service import (
    UNAVAILABLE_WEATHER_ADVICE,
    build_weather_advice,
)
from langchain_core.messages import AIMessageChunk

QUERY_TIME = datetime(2026, 7, 20, tzinfo=UTC)


def planning_request() -> TripPlanningRequest:
    return TripPlanningRequest(
        core=CityTripRequest(
            destination_city="成都",
            duration_days=1,
            start_date=date(2026, 7, 25),
        )
    )


def capability_plan(
    *,
    transport: bool = False,
    hotel: bool = False,
) -> CapabilityPlan:
    return CapabilityPlan(
        transport=TransportCapabilityPlan(enabled=transport),
        hotel=HotelCapabilityPlan(enabled=hotel),
    )


def map_weather_bundle(
    status: str = "usable",
) -> MapWeatherEvidenceBundle:
    place = MapPlaceEvidence(
        reference_id="poi_a1",
        poi_id="a1",
        name="地点 a1",
        address="地址 a1",
        poi_type="风景名胜",
        location=AmapCoordinate(longitude=104.0, latitude=30.0),
        city="成都市",
        search_query="景点",
        search_rank=1,
        estimated_visit_minutes=90,
        candidate_score=42,
    )
    map_evidence = MapTripEvidence(
        city="成都",
        planning_run_id="test-run",
        queried_at=QUERY_TIME,
        days=[
            MapDayEvidence(
                day_index=1,
                date=date(2026, 7, 25),
                attractions=[place],
                estimated_visit_minutes=90,
            )
        ],
    )
    weather = TripWeatherEvidence(
        city="成都",
        queried_at=QUERY_TIME,
        days=[
            DailyWeatherEvidence(
                date=date(2026, 7, 25),
                coverage="available",
                day_weather="晴",
                night_weather="多云",
                day_temperature="32",
                night_temperature="23",
            )
        ],
    )
    if status == "failed":
        return MapWeatherEvidenceBundle(
            status="failed",
            warnings=["地图查询失败"],
            error_code="MAP_FAILED",
        )
    return MapWeatherEvidenceBundle(
        status=status,  # type: ignore[arg-type]
        map=map_evidence,
        weather=weather,
        warnings=["部分天气不可用"] if status == "partial" else [],
    )


def raw_evidence(
    capability: str,
    status: EvidenceStatus,
    *,
    data: object | None = None,
    display_options: list[str] | None = None,
    warning: str | None = None,
) -> RawCapabilityEvidence:
    return RawCapabilityEvidence(
        capability=capability,  # type: ignore[arg-type]
        status=status,
        query={"destination": "成都"},
        queried_at=QUERY_TIME,
        duration_ms=25,
        data=data,
        display_options=display_options or [],
        warnings=[warning] if warning else [],
        error_code="UPSTREAM_TIMEOUT" if status is EvidenceStatus.FAILED else None,
    )


def skipped(capability: str) -> RawCapabilityEvidence:
    return raw_evidence(capability, EvidenceStatus.SKIPPED)


def narrative(
    *,
    transport_options: list[str] | None = None,
    hotel_options: list[str] | None = None,
) -> TripNarrativePlan:
    return TripNarrativePlan(
        title="成都一日攻略",
        summary="按地图、天气与已启用能力结果整理。",
        days=[
            MapDayNarrative(
                day_index=1,
                date=date(2026, 7, 25),
                theme="城市漫游",
                places=[
                    MapPlaceNarrative(
                        reference_id="poi_a1",
                        recommendation_reason="按既定地图顺序游览。",
                    )
                ],
                weather_advice=["天气较热，注意防晒。"],
            )
        ],
        transport_options=transport_options or [],
        hotel_options=hotel_options or [],
    )


def joined(
    *,
    plan: CapabilityPlan | None = None,
    map_status: str = "usable",
    transport: RawCapabilityEvidence | None = None,
    hotel: RawCapabilityEvidence | None = None,
):
    return join_trip_evidence(
        planning_request(),
        plan or capability_plan(),
        map_weather_bundle(map_status),
        transport or skipped("transport"),
        hotel or skipped("hotel"),
    )


def test_joiner_applies_required_map_and_optional_capability_status_rules() -> None:
    assert joined().overall_status == "usable"
    assert joined(map_status="partial").overall_status == "partial"
    assert joined(map_status="failed").overall_status == "failed"

    empty_transport = joined(
        plan=capability_plan(transport=True),
        transport=raw_evidence("transport", EvidenceStatus.EMPTY),
    )
    assert empty_transport.overall_status == "partial"

    failed_hotel = joined(
        plan=capability_plan(hotel=True),
        hotel=raw_evidence(
            "hotel",
            EvidenceStatus.FAILED,
            warning="酒店暂时不可用",
        ),
    )
    assert failed_hotel.overall_status == "partial"
    assert failed_hotel.warnings == ["酒店暂时不可用"]


def test_generation_prompt_exposes_compact_verified_presentation_context() -> None:
    evidence = joined(
        plan=capability_plan(transport=True, hotel=True),
        transport=raw_evidence(
            "transport",
            EvidenceStatus.USABLE,
            data={"opaque": {"flight": "CA1234"}},
            display_options=[
                "去程航班 CA1234｜参考价 ¥680｜[查看详情](https://provider.example/secret)"
            ],
        ),
        hotel=raw_evidence(
            "hotel",
            EvidenceStatus.FAILED,
            data={"must_not_reach_model": "hotel-secret"},
            display_options=["失败状态下不应暴露的旧酒店选项"],
        ),
    )

    prompt = json.loads(build_trip_generation_prompt(evidence))
    serialized = json.dumps(prompt, ensure_ascii=False)

    assert set(prompt) == {"trip", "transport", "hotel", "days", "warnings"}
    assert set(prompt["trip"]) == {
        "origin_city",
        "destination_city",
        "start_date",
        "end_date",
        "duration_days",
        "interests",
        "food_preferences",
        "assumptions",
    }
    assert set(prompt["transport"]) == {
        "enabled",
        "status",
        "modes",
        "journey_scope",
        "origin",
        "destination",
        "outbound_date",
        "return_date",
        "options",
        "warnings",
        "error_code",
    }
    assert prompt["transport"]["options"] == ["去程航班 CA1234｜参考价 ¥680"]
    assert set(prompt["hotel"]) == {
        "enabled",
        "status",
        "destination",
        "check_in_date",
        "check_out_date",
        "nearby_poi",
        "options",
        "warnings",
        "error_code",
    }
    assert prompt["hotel"]["status"] == "failed"
    assert set(prompt["days"][0]) == {
        "day_index",
        "date",
        "weekday",
        "weather",
        "estimated_visit_minutes",
        "estimated_transport_minutes",
        "places",
        "route_legs",
        "warnings",
    }
    assert prompt["days"][0]["weather"] == {
        "coverage": "available",
        "day_weather": "晴",
        "night_weather": "多云",
        "day_temperature": "32",
        "night_temperature": "23",
        "advice": [
            "预报含晴天，户外活动请注意防晒。",
            "气温可能偏高，请避免长时间暴晒并及时补水。",
            "昼夜温差较明显，建议分层穿着。",
        ],
    }
    assert set(prompt["days"][0]["places"][0]) == {
        "reference_id",
        "name",
        "address",
        "poi_type",
        "estimated_visit_minutes",
        "matched_preferences",
        "selection_reasons",
    }
    for excluded in (
        "poi_id",
        "location",
        "candidate_score",
        "opaque",
        "hotel-secret",
        "失败状态下不应暴露",
        "provider.example",
    ):
        assert excluded not in serialized


def test_generation_prompt_compacts_route_legs_without_navigation_steps() -> None:
    evidence = joined()
    map_evidence = evidence.map_weather.map
    assert map_evidence is not None
    day = map_evidence.days[0]
    second = day.attractions[0].model_copy(
        update={
            "reference_id": "poi_a2",
            "poi_id": "a2",
            "name": "地点 a2",
        }
    )
    day.attractions.append(second)
    day.route_legs.append(
        RouteLegEvidence(
            origin_ref="poi_a1",
            destination_ref="poi_a2",
            mode="transit",
            distance_meters=2_578,
            duration_seconds=1_261,
            transfer_count=0,
            route_summary="不应进入模型的逐步导航内容",
            is_fallback=False,
        )
    )

    prompt = json.loads(build_trip_generation_prompt(evidence))

    assert prompt["days"][0]["route_legs"] == [
        {
            "origin_ref": "poi_a1",
            "origin_name": "地点 a1",
            "destination_ref": "poi_a2",
            "destination_name": "地点 a2",
            "mode": "transit",
            "distance_meters": 2_578,
            "duration_minutes": 21,
            "transfer_count": 0,
            "is_fallback": False,
        }
    ]
    assert "逐步导航" not in json.dumps(prompt, ensure_ascii=False)


class FakeStreamingModel:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.calls: list[object] = []
        self.options: list[dict[str, object]] = []
        self.model_name = "qwen3.7-flash"

    async def astream(self, messages: object, **kwargs: object):
        self.calls.append(messages)
        self.options.append(kwargs)
        for chunk in self.chunks:
            yield AIMessageChunk(content=chunk)


@pytest.mark.asyncio
async def test_skeleton_is_deterministic_valid_and_renderable_before_model_generation() -> None:
    evidence = joined()

    update = await BuildItinerarySkeletonNode()(
        {"joined_evidence": evidence}  # type: ignore[arg-type]
    )

    assert update["skeleton_validation_issues"] == []
    assert update["narrative_skeleton"].title == "成都1日旅行方案"
    assert update["narrative_skeleton"].days[0].date == date(2026, 7, 25)
    assert "地点 a1" in update["skeleton_answer"]
    assert "已根据本次查询结果整理" in update["skeleton_answer"]


@pytest.mark.asyncio
async def test_markdown_generation_yields_provider_chunks_without_waiting_for_full_output() -> None:
    evidence = joined()
    model = FakeStreamingModel(
        [
            "# 成都一日攻略\n\n",
            "## Day 1\n\n",
            "地点 a1。",
        ]
    )
    generator = TripItineraryGenerator(
        model,  # type: ignore[arg-type]
        timeout_seconds=1,
    )

    chunks = [chunk async for chunk in generator.stream_markdown(evidence)]

    assert chunks == ["# 成都一日攻略\n\n", "## Day 1\n\n", "地点 a1。"]
    assert len(model.calls) == 1
    messages = model.calls[0]
    assert isinstance(messages, list)
    assert "可以直接收藏和照着走的成品攻略" in messages[0].content
    assert "不同天之间使用 `---` 分隔" in messages[0].content
    assert '"destination_city":"成都"' in messages[1].content
    assert model.options == [
        {
            "temperature": 0,
            "extra_body": {"enable_thinking": False},
        }
    ]


@pytest.mark.asyncio
async def test_skeleton_preserves_optional_failure_degradation() -> None:
    plan = capability_plan(transport=True, hotel=True)
    state = {
        "request": planning_request(),
        "capability_plan": plan,
        "map_weather_evidence": map_weather_bundle(),
        "transport_evidence": raw_evidence(
            "transport",
            EvidenceStatus.USABLE,
            data={"trains": [{"number": "G123"}]},
            display_options=["去程火车 G123｜成都方向"],
        ),
        "hotel_evidence": raw_evidence(
            "hotel",
            EvidenceStatus.FAILED,
            warning="酒店服务超时",
        ),
    }
    joined_update = await EvidenceJoinNode()(state)  # type: ignore[arg-type]
    assert joined_update["joined_evidence"].overall_status == "partial"

    skeleton_update = await BuildItinerarySkeletonNode()(
        {
            **state,
            **joined_update,
        }  # type: ignore[arg-type]
    )

    answer = skeleton_update["skeleton_answer"]
    assert "去程火车 G123｜成都方向" in answer
    assert "酒店查询暂时失败（UPSTREAM_TIMEOUT）" in answer
    assert "地图与天气行程仍可继续使用" in answer
    generated = skeleton_update["narrative_skeleton"]
    assert generated.days[0].date == date(2026, 7, 25)
    assert [place.reference_id for place in generated.days[0].places] == ["poi_a1"]
    assert generated.transport_options == ["去程火车 G123｜成都方向"]
    assert generated.hotel_options == []


def test_skeleton_injects_deterministic_weather_before_validation() -> None:
    evidence = joined()

    generated = build_trip_narrative_skeleton(evidence)

    weather = evidence.map_weather.weather
    assert weather is not None
    assert generated.days[0].weather_advice == build_weather_advice(weather.days[0])
    assert TripPlanValidator().validate(evidence, generated) == []


def test_skeleton_fills_days_from_deterministic_evidence() -> None:
    evidence = joined()

    generated = build_trip_narrative_skeleton(evidence)

    assert generated.days[0].day_index == 1
    assert generated.days[0].date == date(2026, 7, 25)
    assert generated.days[0].theme == "成都第 1 天行程"
    assert [place.reference_id for place in generated.days[0].places] == ["poi_a1"]
    assert generated.days[0].places[0].recommendation_reason == (
        "地点 a1已由后端纳入当天的既定游览顺序。"
    )
    assert TripPlanValidator().validate(evidence, generated) == []


def test_renderer_is_deterministic_for_optional_statuses_without_reliability_copy() -> None:
    evidence = joined(
        plan=capability_plan(transport=True, hotel=True),
        transport=raw_evidence(
            "transport",
            EvidenceStatus.USABLE,
            data={"opaque": True},
            display_options=["航班 CA1234，按查询结果展示"],
        ),
        hotel=raw_evidence("hotel", EvidenceStatus.EMPTY),
    )

    answer = render_trip_itinerary(
        evidence,
        narrative(transport_options=["航班 CA1234，按查询结果展示"]),
    )

    assert "## 城际交通结果" in answer
    assert "航班 CA1234，按查询结果展示" in answer
    assert "## 酒店结果" in answer
    assert "未查询到符合当前条件的酒店结果" in answer
    assert answer.count("数据来源：FlyAI；查询时间：") == 2
    assert "本次未查询机票、火车、酒店" not in answer
    assert "字段级校验" not in answer
    assert "未经校验" not in answer


def test_renderer_splits_complete_answer_into_lossless_semantic_chunks() -> None:
    answer = render_trip_itinerary(joined(), narrative())

    chunks = split_trip_itinerary_sections(answer)

    assert len(chunks) > 1
    assert chunks[0].startswith("# ")
    assert all(chunk.startswith("## ") for chunk in chunks[1:])
    assert "".join(chunks) == answer


def test_renderer_keeps_existing_map_only_scope_when_optional_capabilities_are_disabled() -> None:
    answer = render_trip_itinerary(joined(), narrative())

    assert "本次未查询机票、火车、酒店、价格、库存、营业状态或预订信息" in answer
    assert "## 城际交通结果" not in answer
    assert "## 酒店结果" not in answer


def test_renderer_uses_fixed_fallback_for_uncovered_weather_date() -> None:
    evidence = joined()
    weather = evidence.map_weather.weather
    assert weather is not None
    weather.days[0].coverage = "unavailable"
    weather.days[0].unavailable_reason = "供应商返回了一段不稳定说明。"
    model_plan = narrative()
    model_plan.days[0].weather_advice = ["模型猜测当天有雨，降雨概率 50%。"]

    answer = render_trip_itinerary(evidence, model_plan)

    assert "**天气**：该日期暂无对应天气预报。" in answer
    assert UNAVAILABLE_WEATHER_ADVICE in answer
    assert "供应商返回了一段不稳定说明" not in answer
    assert "降雨概率 50%" not in answer
