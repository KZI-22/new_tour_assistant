from __future__ import annotations

from datetime import date

import pytest
from app.schemas.itinerary import ItineraryPlan, TripRequest
from pydantic import ValidationError


def test_trip_request_completes_dates_and_round_trips_json() -> None:
    request = TripRequest(
        origin=" 南京 ",
        destinations=[" 杭州 ", "杭州"],
        start_date=date(2026, 7, 20),
        duration_days=3,
        adults=2,
        total_budget=4000,
        interests=["自然", "人文", "自然"],
        pace="relaxed",
    )

    assert request.end_date == date(2026, 7, 22)
    assert request.traveler_count == 2
    assert request.destinations == ["杭州"]
    assert request.interests == ["自然", "人文"]
    assert TripRequest.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize("field", ["total_budget", "hotel_budget_per_night"])
def test_trip_request_rejects_negative_budgets(field: str) -> None:
    with pytest.raises(ValidationError):
        TripRequest.model_validate({field: -1})


def test_itinerary_plan_serializes_dates_for_jsonb() -> None:
    plan = ItineraryPlan(
        title="杭州两日游",
        destination="杭州",
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
        days=[],
    )

    document = plan.model_dump(mode="json")

    assert document["start_date"] == "2026-07-20"
    assert ItineraryPlan.model_validate(document) == plan
