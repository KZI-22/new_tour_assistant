from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from app.schemas.map_planning import MapTripEvidence
from app.schemas.trip_capabilities import (
    CapabilityAction,
    CapabilityPlan,
    HotelIntent,
    JourneyScope,
    RequirementCheck,
    TransportIntent,
    TransportMode,
    TripPlanningRequest,
)
from app.schemas.trip_evidence import (
    EvidenceStatus,
    JoinedTripEvidence,
    MapWeatherEvidenceBundle,
    RawCapabilityEvidence,
)
from app.schemas.trip_validation import ValidationIssue
from pydantic import ValidationError


def test_trip_planning_request_keeps_city_request_compatible() -> None:
    request = TripPlanningRequest(
        core={
            "destination_city": "成都",
            "duration_days": 3,
            "start_date": "2026-08-01",
            "interests": ["历史"],
        },
        transport={
            "action": "enable",
            "modes": ["flight", "train"],
            "journey_scope": "round_trip",
            "origin_city": "北京",
            "max_price": 2_000,
        },
        hotel={"action": "enable", "hotel_stars": [4, 5]},
    )

    assert request.core.destination_city == "成都"
    assert request.core.start_date == date(2026, 8, 1)
    assert request.transport.action is CapabilityAction.ENABLE
    assert request.transport.modes == [TransportMode.FLIGHT, TransportMode.TRAIN]
    assert request.transport.journey_scope is JourneyScope.ROUND_TRIP
    assert request.hotel == HotelIntent(
        action=CapabilityAction.ENABLE,
        hotel_stars=[4, 5],
    )


def test_optional_capabilities_default_to_unspecified_and_disabled() -> None:
    request = TripPlanningRequest(core={})
    plan = CapabilityPlan()

    assert request.transport == TransportIntent()
    assert request.hotel == HotelIntent()
    assert plan.map_weather_enabled is True
    assert plan.transport.enabled is False
    assert plan.hotel.enabled is False


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (TransportIntent, {"max_price": 0}),
        (HotelIntent, {"max_nightly_price": -1}),
        (CapabilityPlan, {"map_weather_enabled": False}),
    ],
)
def test_capability_contracts_reject_invalid_invariants(
    model: type[object],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model(**payload)  # type: ignore[operator]


def test_requirement_check_and_validation_issue_are_structured() -> None:
    check = RequirementCheck(
        complete=False,
        missing=[
            {
                "field": "transport.origin",
                "capability": "transport",
                "display_name": "出发城市",
                "reason": "交通查询需要出发城市",
            }
        ],
    )
    issue = ValidationIssue(
        code="MAP_REFERENCE_UNKNOWN",
        path="days.1.places.0.reference_id",
        message="地图引用不存在",
        reference_id="missing-ref",
    )

    assert check.missing[0].capability == "transport"
    assert issue.model_dump()["severity"] == "error"


def test_joined_evidence_preserves_opaque_flyai_data() -> None:
    request = TripPlanningRequest(
        core={
            "destination_city": "成都",
            "duration_days": 1,
            "start_date": "2026-08-01",
        }
    )
    map_evidence = MapTripEvidence(
        city="成都",
        planning_run_id="run-1",
        days=[],
    )
    opaque_data = {
        "provider_shape": {
            "items": [{"unexpected": ["nested", {"value": 1}]}],
        }
    }
    transport = RawCapabilityEvidence(
        capability="transport",
        status=EvidenceStatus.USABLE,
        query={"modes": ["flight"]},
        queried_at=datetime(2026, 7, 23, tzinfo=UTC),
        duration_ms=12,
        data=opaque_data,
    )
    hotel = RawCapabilityEvidence(
        capability="hotel",
        status=EvidenceStatus.SKIPPED,
        query={},
        queried_at=datetime(2026, 7, 23, tzinfo=UTC),
        duration_ms=0,
    )
    joined = JoinedTripEvidence(
        request=request,
        capabilities=CapabilityPlan(),
        map_weather=MapWeatherEvidenceBundle(status="usable", map=map_evidence),
        transport=transport,
        hotel=hotel,
        overall_status="usable",
    )

    assert joined.transport.data == opaque_data
    assert joined.hotel.status is EvidenceStatus.SKIPPED
