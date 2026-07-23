from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime

import pytest
from app.graphs.trip_planner_nodes import ValidateItineraryNode
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
    JoinedTripEvidence,
    MapWeatherEvidenceBundle,
    RawCapabilityEvidence,
)
from app.schemas.trip_itinerary import TripNarrativePlan
from app.schemas.trip_planning import (
    CityTripRequest,
    DailyWeatherEvidence,
    TripWeatherEvidence,
)
from app.services.trip_plan_validator import TripPlanValidator
from app.services.trip_planner_logging import logged_trip_planner_node

QUERY_TIME = datetime(2026, 7, 20, tzinfo=UTC)
SENSITIVE_PROVIDER_TEXT = "sk-secret-provider-response-123"


def place(reference_id: str, poi_id: str) -> MapPlaceEvidence:
    return MapPlaceEvidence(
        reference_id=reference_id,
        poi_id=poi_id,
        name=f"地点 {poi_id}",
        address=f"地址 {poi_id}",
        poi_type="风景名胜",
        location=AmapCoordinate(longitude=104.0, latitude=30.0),
        city="成都市",
        search_query="景点",
        search_rank=1,
        estimated_visit_minutes=90,
        candidate_score=42,
    )


def raw_evidence(
    capability: str,
    status: EvidenceStatus,
) -> RawCapabilityEvidence:
    return RawCapabilityEvidence(
        capability=capability,  # type: ignore[arg-type]
        status=status,
        query={"destination": "成都"},
        queried_at=QUERY_TIME,
        duration_ms=25,
        data={"raw": SENSITIVE_PROVIDER_TEXT},
        display_options=(
            [f"{'具体班次 G123' if capability == 'transport' else '具体酒店 A'}"]
            if status is EvidenceStatus.USABLE
            else []
        ),
        error_code="UPSTREAM_TIMEOUT" if status is EvidenceStatus.FAILED else None,
    )


def joined_evidence(
    *,
    transport_enabled: bool = False,
    transport_status: EvidenceStatus = EvidenceStatus.SKIPPED,
    hotel_enabled: bool = False,
    hotel_status: EvidenceStatus = EvidenceStatus.SKIPPED,
) -> JoinedTripEvidence:
    places = [place("poi_a1", "a1"), place("poi_a2", "a2")]
    map_evidence = MapTripEvidence(
        city="成都",
        planning_run_id="map-run",
        queried_at=QUERY_TIME,
        days=[
            MapDayEvidence(
                day_index=1,
                date=date(2026, 7, 25),
                attractions=places,
                estimated_visit_minutes=180,
                route_legs=[
                    RouteLegEvidence(
                        origin_ref="poi_a1",
                        destination_ref="poi_a2",
                        mode="walking",
                        distance_meters=500,
                        duration_seconds=480,
                    )
                ],
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
    return JoinedTripEvidence(
        request=TripPlanningRequest(
            core=CityTripRequest(
                destination_city="成都",
                duration_days=1,
                start_date=date(2026, 7, 25),
            )
        ),
        capabilities=CapabilityPlan(
            transport=TransportCapabilityPlan(enabled=transport_enabled),
            hotel=HotelCapabilityPlan(enabled=hotel_enabled),
        ),
        map_weather=MapWeatherEvidenceBundle(
            status="usable",
            map=map_evidence,
            weather=weather,
        ),
        transport=raw_evidence("transport", transport_status),
        hotel=raw_evidence("hotel", hotel_status),
        overall_status="usable",
    )


def valid_plan() -> TripNarrativePlan:
    return TripNarrativePlan(
        title="成都一日攻略",
        summary="按已启用能力结果整理。",
        days=[
            MapDayNarrative(
                day_index=1,
                date=date(2026, 7, 25),
                theme="城市漫游",
                places=[
                    MapPlaceNarrative(
                        reference_id=reference_id,
                        recommendation_reason="按既定路线游览。",
                    )
                    for reference_id in ("poi_a1", "poi_a2")
                ],
                weather_advice=["晴天注意防晒，最高温度 32℃。"],
            )
        ],
    )


def codes(
    evidence: JoinedTripEvidence,
    plan: TripNarrativePlan,
) -> set[str]:
    return {issue.code for issue in TripPlanValidator().validate(evidence, plan)}


def test_valid_trip_plan_has_no_deterministic_issues() -> None:
    assert codes(joined_evidence(), valid_plan()) == set()


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("day_index", "DAY_INDEX_MISMATCH"),
        ("day_date", "DAY_DATE_MISMATCH"),
        ("reference_order", "MAP_REFERENCE_ORDER_MISMATCH"),
        ("reference_unknown", "MAP_REFERENCE_UNKNOWN"),
        ("duplicate_poi", "DUPLICATE_POI"),
        ("route_endpoint", "ROUTE_ENDPOINT_MISMATCH"),
        ("weather_date", "WEATHER_DATE_MISMATCH"),
        ("weather_without_evidence", "WEATHER_FACT_WITHOUT_EVIDENCE"),
    ],
)
def test_map_weather_validation_codes(case: str, expected_code: str) -> None:
    evidence = joined_evidence()
    plan = valid_plan()
    map_evidence = evidence.map_weather.map
    weather = evidence.map_weather.weather
    assert map_evidence is not None
    assert weather is not None

    if case == "day_index":
        plan.days[0].day_index = 2
    elif case == "day_date":
        plan.days[0].date = date(2026, 7, 26)
    elif case == "reference_order":
        plan.days[0].places.reverse()
    elif case == "reference_unknown":
        plan.days[0].places[0].reference_id = "unknown_reference"
    elif case == "duplicate_poi":
        map_evidence.days[0].attractions[1].poi_id = "a1"
    elif case == "route_endpoint":
        map_evidence.days[0].route_legs[0].destination_ref = "poi_a1"
    elif case == "weather_date":
        weather.days[0].date = date(2026, 7, 26)
    elif case == "weather_without_evidence":
        weather.days[0].coverage = "unavailable"

    assert expected_code in codes(evidence, plan)


@pytest.mark.parametrize(
    (
        "capability",
        "enabled",
        "status",
        "expected_code",
    ),
    [
        (
            "transport",
            False,
            EvidenceStatus.SKIPPED,
            "TRANSPORT_OUTPUT_WHILE_DISABLED",
        ),
        (
            "transport",
            True,
            EvidenceStatus.FAILED,
            "TRANSPORT_FACT_WITHOUT_USABLE_EVIDENCE",
        ),
        (
            "hotel",
            False,
            EvidenceStatus.SKIPPED,
            "HOTEL_OUTPUT_WHILE_DISABLED",
        ),
        (
            "hotel",
            True,
            EvidenceStatus.EMPTY,
            "HOTEL_FACT_WITHOUT_USABLE_EVIDENCE",
        ),
    ],
)
def test_optional_capability_output_validation_codes(
    capability: str,
    enabled: bool,
    status: EvidenceStatus,
    expected_code: str,
) -> None:
    evidence = joined_evidence(
        transport_enabled=enabled if capability == "transport" else False,
        transport_status=status if capability == "transport" else EvidenceStatus.SKIPPED,
        hotel_enabled=enabled if capability == "hotel" else False,
        hotel_status=status if capability == "hotel" else EvidenceStatus.SKIPPED,
    )
    plan = valid_plan()
    if capability == "transport":
        plan.transport_options = ["具体班次 G123"]
    else:
        plan.hotel_options = ["具体酒店 A"]

    assert expected_code in codes(evidence, plan)


@pytest.mark.parametrize(
    ("capability", "missing_code", "mismatch_code"),
    [
        (
            "transport",
            "TRANSPORT_USABLE_EVIDENCE_WITHOUT_OPTIONS",
            "TRANSPORT_OPTION_MISMATCH",
        ),
        (
            "hotel",
            "HOTEL_USABLE_EVIDENCE_WITHOUT_OPTIONS",
            "HOTEL_OPTION_MISMATCH",
        ),
    ],
)
def test_usable_optional_evidence_requires_matching_normalized_options(
    capability: str,
    missing_code: str,
    mismatch_code: str,
) -> None:
    evidence = joined_evidence(
        transport_enabled=capability == "transport",
        transport_status=(
            EvidenceStatus.USABLE if capability == "transport" else EvidenceStatus.SKIPPED
        ),
        hotel_enabled=capability == "hotel",
        hotel_status=(EvidenceStatus.USABLE if capability == "hotel" else EvidenceStatus.SKIPPED),
    )
    plan = valid_plan()
    assert mismatch_code in codes(evidence, plan)

    normalized = (
        evidence.transport.display_options
        if capability == "transport"
        else evidence.hotel.display_options
    )
    if capability == "transport":
        plan.transport_options = list(normalized)
        evidence.transport.display_options = []
    else:
        plan.hotel_options = list(normalized)
        evidence.hotel.display_options = []
    assert missing_code in codes(evidence, plan)


def test_booking_completion_claim_is_forbidden() -> None:
    plan = valid_plan()
    plan.summary = "酒店已经预订成功。"

    assert "BOOKING_CLAIM_FORBIDDEN" in codes(joined_evidence(), plan)


@pytest.mark.asyncio
async def test_validation_failure_logs_every_safe_issue_for_both_attempts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    evidence = joined_evidence()
    plan = valid_plan()
    plan.days[0].places[0].reference_id = SENSITIVE_PROVIDER_TEXT
    node = ValidateItineraryNode()
    caplog.set_level(logging.INFO)

    first = await node(
        {
            "planning_run_id": "planner-run-123",
            "revision_count": 0,
            "joined_evidence": evidence,
            "narrative": plan,
        }
    )
    second = await node(
        {
            "planning_run_id": "planner-run-123",
            "revision_count": 1,
            "joined_evidence": evidence,
            "narrative": plan,
        }
    )

    assert first["validation_issues"]
    assert second["validation_issues"]
    assert caplog.text.count("event=trip_plan_validation_failed") == 2
    assert "planning_run_id=planner-run-123" in caplog.text
    assert "revision_count=0" in caplog.text
    assert "revision_count=1" in caplog.text
    assert "code=MAP_REFERENCE_UNKNOWN" in caplog.text
    assert "path=days.1.places.0.reference_id" in caplog.text
    assert "reference_id=sha256:" in caplog.text
    assert SENSITIVE_PROVIDER_TEXT not in caplog.text


@pytest.mark.asyncio
async def test_node_logs_success_failure_and_cancellation_without_sensitive_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    async def success(_: object) -> dict[str, object]:
        return {}

    async def failure(_: object) -> dict[str, object]:
        raise RuntimeError(SENSITIVE_PROVIDER_TEXT)

    async def cancellation(_: object) -> dict[str, object]:
        raise asyncio.CancelledError

    state = {"planning_run_id": "planner-run-456"}
    await logged_trip_planner_node("success_node", success)(state)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await logged_trip_planner_node("failure_node", failure)(state)  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await logged_trip_planner_node("cancel_node", cancellation)(state)  # type: ignore[arg-type]

    assert "event=trip_planner_node_started" in caplog.text
    assert "node=success_node status=completed" in caplog.text
    assert "node=failure_node status=failed" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert "node=cancel_node status=cancelled" in caplog.text
    assert "node=cancel_node status=failed" not in caplog.text
    assert SENSITIVE_PROVIDER_TEXT not in caplog.text
