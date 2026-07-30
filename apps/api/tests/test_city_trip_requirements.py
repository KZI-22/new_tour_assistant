from __future__ import annotations

from datetime import date

import pytest
from app.schemas.trip_planning import CityTripRequest
from app.services.city_trip_request import (
    explicit_dates,
    explicit_duration_candidates,
    validate_city_trip_request,
)


def test_explicit_start_date_supports_stable_formats_and_simple_relative_dates() -> None:
    today = date(2026, 7, 20)

    assert explicit_dates("2026-07-25 出发", today=today) == [date(2026, 7, 25)]
    assert explicit_dates("2026年7月25日开始", today=today) == [date(2026, 7, 25)]
    assert explicit_dates("2026年七月二十二日开始", today=today) == [date(2026, 7, 22)]
    assert explicit_dates("7月25日开始", today=today) == [date(2026, 7, 25)]
    assert explicit_dates("七月22日开始", today=today) == [date(2026, 7, 22)]
    assert explicit_dates("7月二十二日开始", today=today) == [date(2026, 7, 22)]
    assert explicit_dates("后天出发", today=today) == [date(2026, 7, 22)]
    assert explicit_dates("下周五出发", today=today) == [date(2026, 7, 31)]
    assert explicit_dates("下周末出发", today=today) == [date(2026, 8, 1)]
    assert explicit_dates("下周找一天", today=today) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("规划南京三日游", 3),
        ("规划南京三天游", 3),
        ("改成四天吧", 4),
        ("行程五日", 5),
        ("五日行程", 5),
        ("从7月22日开始", None),
        ("从七月22日开始", None),
        ("从2026年7月22日开始", None),
        ("把第 2 天安排得轻松一点", None),
        ("7月22日开始，玩三天", 3),
        ("七月22日开始，南京三日游", 3),
    ],
)
def test_explicit_duration_requires_duration_context(text: str, expected: int | None) -> None:
    candidates = explicit_duration_candidates(text)
    assert (candidates[-1] if candidates else None) == expected


def test_required_fields_reports_missing_start_date() -> None:
    missing, errors = validate_city_trip_request(
        CityTripRequest(destination_city="成都", duration_days=3),
        maximum_days=10,
    )

    assert missing == ["start_date"]
    assert errors == []
