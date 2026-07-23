from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from app.graphs.trip_planner_nodes import (
    EvidenceJoinNode,
    GenerateItineraryNode,
    RenderResponseNode,
)
from app.schemas.amap import AmapCoordinate
from app.schemas.map_planning import (
    MapDayEvidence,
    MapDayNarrative,
    MapPlaceEvidence,
    MapPlaceNarrative,
    MapTripEvidence,
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
)
from app.services.trip_itinerary_renderer import render_trip_itinerary

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


def test_generation_prompt_only_exposes_normalized_enabled_options() -> None:
    evidence = joined(
        plan=capability_plan(transport=True, hotel=True),
        transport=raw_evidence(
            "transport",
            EvidenceStatus.USABLE,
            data={"opaque": {"flight": "CA1234"}},
            display_options=["去程航班 CA1234｜参考价 ¥680"],
        ),
        hotel=raw_evidence(
            "hotel",
            EvidenceStatus.FAILED,
            data={"must_not_reach_model": "hotel-secret"},
        ),
    )

    prompt = json.loads(build_trip_generation_prompt(evidence, validation_issues=[]))

    assert prompt["transport_evidence"]["display_options"] == ["去程航班 CA1234｜参考价 ¥680"]
    assert prompt["hotel_evidence"]["display_options"] == []
    assert "opaque" not in json.dumps(prompt, ensure_ascii=False)
    assert "hotel-secret" not in json.dumps(prompt, ensure_ascii=False)

    disabled = joined(
        transport=raw_evidence(
            "transport",
            EvidenceStatus.USABLE,
            data={"must_not_reach_model": "transport-secret"},
            display_options=["不应进入提示词"],
        )
    )
    disabled_prompt = json.loads(build_trip_generation_prompt(disabled, validation_issues=[]))
    assert disabled_prompt["transport_evidence"]["display_options"] == []
    assert disabled_prompt["transport_evidence"]["query"] == {}


class FakeRunnable:
    def __init__(self, value: TripNarrativePlan, calls: list[object]) -> None:
        self._value = value
        self._calls = calls

    async def ainvoke(self, messages: object) -> TripNarrativePlan:
        self._calls.append(messages)
        return self._value


class FakeModel:
    def __init__(self, value: TripNarrativePlan) -> None:
        self.value = value
        self.calls: list[object] = []

    def with_structured_output(self, _: type[object]) -> FakeRunnable:
        return FakeRunnable(self.value, self.calls)


@pytest.mark.asyncio
async def test_generation_and_graph_nodes_preserve_optional_failure_degradation() -> None:
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

    expected = narrative()
    model = FakeModel(expected)
    generation_update = await GenerateItineraryNode(
        TripItineraryGenerator(
            model,  # type: ignore[arg-type]
            timeout_seconds=1,
        )
    )(
        {
            **state,
            **joined_update,
        }  # type: ignore[arg-type]
    )
    render_update = await RenderResponseNode()(
        {
            **state,
            **joined_update,
            **generation_update,
        }  # type: ignore[arg-type]
    )

    answer = render_update["final_answer"]
    assert "去程火车 G123｜成都方向" in answer
    assert "酒店查询暂时失败（UPSTREAM_TIMEOUT）" in answer
    assert "地图与天气行程仍可继续使用" in answer
    assert model.calls


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


def test_renderer_keeps_existing_map_only_scope_when_optional_capabilities_are_disabled() -> None:
    answer = render_trip_itinerary(joined(), narrative())

    assert "本次未查询机票、火车、酒店、价格、库存、营业状态或预订信息" in answer
    assert "## 城际交通结果" not in answer
    assert "## 酒店结果" not in answer
