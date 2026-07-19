from __future__ import annotations

from app.graphs.xhs_trip_planner import (
    _latest_explicit_duration_days,
    build_search_keyword,
)
from app.schemas.chat import ChatMessage


def test_build_search_keyword_uses_only_city_and_duration() -> None:
    assert build_search_keyword(" 成都 ", 3) == "成都 3天 旅游攻略"


def test_latest_duration_does_not_treat_ordinal_day_as_trip_length() -> None:
    messages = [
        ChatMessage(role="user", content="帮我规划成都三天游"),
        ChatMessage(role="assistant", content="旧攻略"),
        ChatMessage(role="user", content="把第二天安排得轻松一点"),
    ]

    assert _latest_explicit_duration_days(messages) is None


def test_latest_explicit_duration_overrides_previous_duration() -> None:
    messages = [
        ChatMessage(role="user", content="帮我规划成都三天游"),
        ChatMessage(role="assistant", content="旧攻略"),
        ChatMessage(role="user", content="改成四天吧"),
    ]

    assert _latest_explicit_duration_days(messages) == 4
