from __future__ import annotations

from datetime import date, datetime

import pytest
from app.core.request_context import use_request_context
from app.schemas.chat import ChatMessage
from app.schemas.context import CurrentTimeContext, TravelRequestContext
from app.schemas.trip_planning import CityTripRequest
from app.services.city_trip_request import (
    apply_explicit_request_overrides,
    clarification_question,
    explicit_duration_days,
    explicit_start_date,
    request_extraction_prompt,
    validate_city_trip_request,
)


def test_explicit_start_date_supports_stable_formats_and_simple_relative_dates() -> None:
    today = date(2026, 7, 20)

    assert explicit_start_date("2026-07-25 出发", today=today) == date(2026, 7, 25)
    assert explicit_start_date("2026年7月25日开始", today=today) == date(2026, 7, 25)
    assert explicit_start_date("2026年七月二十二日开始", today=today) == date(2026, 7, 22)
    assert explicit_start_date("7月25日开始", today=today) == date(2026, 7, 25)
    assert explicit_start_date("七月22日开始", today=today) == date(2026, 7, 22)
    assert explicit_start_date("7月二十二日开始", today=today) == date(2026, 7, 22)
    assert explicit_start_date("后天出发", today=today) == date(2026, 7, 22)
    assert explicit_start_date("下周五出发", today=today) == date(2026, 7, 31)
    assert explicit_start_date("下周末出发", today=today) == date(2026, 8, 1)
    assert explicit_start_date("下周找一天", today=today) is None


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
    assert explicit_duration_days(text) == expected


def test_date_follow_up_does_not_override_duration_and_uses_current_year() -> None:
    context = TravelRequestContext(
        client_ip=None,
        client_ip_is_public_ipv4=False,
        time=CurrentTimeContext(
            current_datetime=datetime.fromisoformat("2026-07-20T14:13:28+08:00"),
            current_date=date(2026, 7, 20),
            timezone="Asia/Shanghai",
            weekday="Monday",
        ),
    )
    messages = [
        ChatMessage(role="user", content="规划去南京的三日游攻略"),
        ChatMessage(role="assistant", content="请告诉我计划从哪一天开始游玩。"),
        ChatMessage(role="user", content="从七月22日开始"),
    ]
    model_request = CityTripRequest(
        destination_city="南京",
        duration_days=3,
        start_date=date(2025, 7, 22),
    )

    with use_request_context(context):
        request, overrides = apply_explicit_request_overrides(model_request, messages)
        prompt = request_extraction_prompt(messages)

    assert explicit_duration_days("从七月22日开始") is None
    assert request.duration_days == 3
    assert request.start_date == date(2026, 7, 22)
    assert overrides == {
        "explicit_duration_override": False,
        "explicit_start_date_override": True,
    }
    assert "当前日期是 2026-07-20，时区是 Asia/Shanghai" in prompt


def test_required_fields_merge_clarification_and_date_only_prompt() -> None:
    missing, errors = validate_city_trip_request(
        CityTripRequest(destination_city="成都", duration_days=3),
        maximum_days=10,
    )

    assert missing == ["start_date"]
    assert errors == []
    assert clarification_question(missing, errors) == (
        "请告诉我计划从哪一天开始游玩，例如“7 月 25 日开始”。"
    )
