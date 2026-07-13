from __future__ import annotations

from datetime import datetime

import pytest
from app.services.travel_dates import normalize_travel_dates, validate_travel_dates

NOW = datetime.fromisoformat("2026-07-13T10:00:00+08:00")


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("今天出发", "2026-07-13"),
        ("明天去杭州", "2026-07-14"),
        ("后天去苏州", "2026-07-15"),
        ("2026-08-01去北京", "2026-08-01"),
        ("2026年8月2日去上海", "2026-08-02"),
    ],
)
def test_normalize_supported_date_expressions(expression: str, expected: str) -> None:
    result = normalize_travel_dates(expression, now=NOW)

    assert result.is_ambiguous is False
    assert result.departure_date is not None
    assert result.departure_date.isoformat() == expected


def test_normalize_nights_calculates_checkout() -> None:
    result = normalize_travel_dates("明天去杭州，住三晚", now=NOW)

    assert result.check_in_date is not None
    assert result.check_in_date.isoformat() == "2026-07-14"
    assert result.check_out_date is not None
    assert result.check_out_date.isoformat() == "2026-07-17"
    assert result.nights == 3


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"departure_date": "2026-07-12"}, "早于当前日期"),
        (
            {"departure_date": "2026-07-15", "return_date": "2026-07-14"},
            "返程日期",
        ),
        (
            {
                "departure_date": "2026-07-15",
                "check_in_date": "2026-07-15",
                "check_out_date": "2026-07-15",
            },
            "退房日期",
        ),
        ({"departure_date": "2026-99-99"}, "日期格式无效"),
        ({"departure_date": None}, "缺少出发日期"),
    ],
)
def test_validate_travel_dates_reports_errors(
    kwargs: dict[str, object],
    message: str,
) -> None:
    result = validate_travel_dates(**kwargs, now=NOW)  # type: ignore[arg-type]

    assert result.is_valid is False
    assert message in result.message


def test_validate_travel_dates_accepts_consistent_stay() -> None:
    result = validate_travel_dates(
        "2026-07-15",
        check_in_date="2026-07-15",
        check_out_date="2026-07-18",
        nights=3,
        now=NOW,
    )

    assert result.is_valid is True
