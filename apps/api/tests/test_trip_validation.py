from __future__ import annotations

from datetime import date, datetime, time

from app.schemas.itinerary import (
    Activity,
    BudgetSummary,
    DayPlan,
    HotelOption,
    ItineraryPlan,
    TransportOption,
    TripRequest,
)
from app.services.trip_validation import validate_itinerary


def _request() -> TripRequest:
    return TripRequest(
        origin="南京",
        destinations=["杭州"],
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
        total_budget=1000,
        pace="relaxed",
    )


def _transport() -> TransportOption:
    return TransportOption(
        transport_type="train",
        departure_city="南京",
        arrival_city="杭州",
        departure_time=datetime(2026, 7, 20, 8, 0),
        arrival_time=datetime(2026, 7, 20, 10, 0),
        train_number="G1",
        price=200,
        source_tool="search_train",
        source_reference="search_train:G1",
    )


def _hotel() -> HotelOption:
    return HotelOption(
        name="西湖酒店",
        check_in_date=date(2026, 7, 20),
        check_out_date=date(2026, 7, 21),
        nightly_price=500,
        source_tool="search_hotel",
        source_reference="search_hotel:h1",
    )


def test_deterministic_validation_reports_time_pace_duplicates_budget_and_sources() -> None:
    transport = _transport()
    hotel = _hotel()
    plan = ItineraryPlan(
        title="杭州两日游",
        origin="南京",
        destination="杭州",
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
        outbound_transport=transport,
        hotel=hotel,
        days=[
            DayPlan(
                date=date(2026, 7, 20),
                day_index=1,
                activities=[
                    Activity(
                        start_time=time(9),
                        end_time=time(11),
                        place_name="西湖",
                        poi_id="p1",
                        activity_type="游览",
                    ),
                    Activity(
                        start_time=time(10, 30),
                        end_time=time(12),
                        place_name="博物馆",
                        poi_id="p2",
                        activity_type="参观",
                    ),
                    Activity(place_name="灵隐寺", poi_id="p3", activity_type="参观"),
                    Activity(place_name="宋城", poi_id="p4", activity_type="游览"),
                ],
            ),
            DayPlan(
                date=date(2026, 7, 21),
                day_index=2,
                activities=[Activity(place_name="西湖", poi_id="p1", activity_type="游览")],
            ),
        ],
        budget=BudgetSummary(total_estimated_cost=1500, user_budget=1000),
    )

    issues = validate_itinerary(
        plan,
        _request(),
        transport_options=[],
        hotel_options=[],
        known_poi_ids={"p1", "p2", "p3", "p4"},
    )
    codes = {issue.code for issue in issues}

    assert {
        "ACTIVITY_BEFORE_ARRIVAL",
        "ACTIVITY_TIME_OVERLAP",
        "TOO_MANY_DAILY_ACTIVITIES",
        "DUPLICATE_ACTIVITY",
        "DUPLICATE_POI",
        "OVER_BUDGET",
        "UNVERIFIED_TRANSPORT_FACT",
        "UNVERIFIED_HOTEL_FACT",
    } <= codes


def test_verified_plan_passes_source_and_date_checks() -> None:
    transport = _transport()
    hotel = _hotel()
    plan = ItineraryPlan(
        title="杭州两日游",
        origin="南京",
        destination="杭州",
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
        outbound_transport=transport,
        hotel=hotel,
        days=[
            DayPlan(date=date(2026, 7, 20), day_index=1),
            DayPlan(date=date(2026, 7, 21), day_index=2),
        ],
    )

    issues = validate_itinerary(
        plan,
        _request(),
        transport_options=[transport],
        hotel_options=[hotel],
        route_data_available=True,
    )

    assert not [issue for issue in issues if issue.severity == "error"]
