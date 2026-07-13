from __future__ import annotations

from datetime import date, timedelta

import pytest
from app.schemas.travel import FlightSearchInput, HotelSearchInput, PoiSearchInput
from pydantic import ValidationError


def future_date(days: int = 1) -> date:
    return date.today() + timedelta(days=days)


def test_flight_rejects_non_iso_date() -> None:
    with pytest.raises(ValidationError, match="departure_date"):
        FlightSearchInput(
            origin="上海",
            destination="北京",
            departure_date="next Friday",
        )


def test_flight_rejects_same_origin_and_destination() -> None:
    with pytest.raises(ValidationError, match="must be different"):
        FlightSearchInput(
            origin=" 上海 ",
            destination="上海",
            departure_date=future_date(),
        )


def test_flight_rejects_past_departure_date() -> None:
    with pytest.raises(ValidationError, match="earlier than today"):
        FlightSearchInput(
            origin="上海",
            destination="北京",
            departure_date=date.today() - timedelta(days=1),
        )


def test_hotel_rejects_check_in_on_or_after_check_out() -> None:
    check_in = future_date(3)
    with pytest.raises(ValidationError, match="earlier than check_out_date"):
        HotelSearchInput(
            destination="杭州",
            check_in_date=check_in,
            check_out_date=check_in,
        )


def test_hotel_rejects_non_positive_price() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        HotelSearchInput(
            destination="杭州",
            check_in_date=future_date(),
            check_out_date=future_date(2),
            max_price=0,
        )


def test_poi_accepts_only_cli_supported_categories() -> None:
    with pytest.raises(ValidationError, match="category"):
        PoiSearchInput(city="杭州", keyword="西湖", category="shopping mall")
