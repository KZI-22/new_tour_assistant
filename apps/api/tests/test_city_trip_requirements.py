from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from app.core.settings import Settings
from app.graphs.xhs_trip_planner import XhsTripPlanner
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import MessageDeltaEvent
from app.schemas.xhs_planning import XhsTripRequest, XhsTripRequestExtraction
from app.services.city_trip_request import (
    clarification_question,
    explicit_start_date,
    validate_city_trip_request,
)


class FakeExtractionInvoker:
    def __init__(self, response: object) -> None:
        self.response = response

    async def ainvoke(self, _: object) -> object:
        return self.response


class FakeExtractionModel:
    def __init__(self, response: object) -> None:
        self.response = response

    def with_structured_output(self, _: object) -> FakeExtractionInvoker:
        return FakeExtractionInvoker(self.response)


class LoginMustNotRun:
    def __init__(self) -> None:
        self.check_login_calls = 0

    async def check_login(self) -> Any:
        self.check_login_calls += 1
        raise AssertionError("login must not run before required fields are complete")


def test_explicit_start_date_supports_stable_formats_and_simple_relative_dates() -> None:
    today = date(2026, 7, 20)

    assert explicit_start_date("2026-07-25 出发", today=today) == date(2026, 7, 25)
    assert explicit_start_date("2026年7月25日开始", today=today) == date(2026, 7, 25)
    assert explicit_start_date("7月25日开始", today=today) == date(2026, 7, 25)
    assert explicit_start_date("后天出发", today=today) == date(2026, 7, 22)
    assert explicit_start_date("下周找一天", today=today) is None


def test_required_fields_merge_clarification_and_date_only_prompt() -> None:
    missing, errors = validate_city_trip_request(
        XhsTripRequest(destination_city="成都", duration_days=3),
        maximum_days=10,
    )

    assert missing == ["start_date"]
    assert errors == []
    assert clarification_question(missing, errors) == (
        "请告诉我计划从哪一天开始游玩，例如“7 月 25 日开始”。"
    )


@pytest.mark.asyncio
async def test_missing_start_date_ends_before_login_or_external_tools() -> None:
    research = LoginMustNotRun()
    planner = XhsTripPlanner(
        research_service=research,  # type: ignore[arg-type]
        settings=Settings(
            app_name="test",
            model_config_path=Path("config/models.yaml"),
            cors_origins=(),
            log_level="INFO",
            trip_planner_enabled=True,
        ),
    )
    model = FakeExtractionModel(
        XhsTripRequestExtraction(request=XhsTripRequest(destination_city="成都", duration_days=3))
    )

    events = [
        event
        async for event in planner.stream(
            model,  # type: ignore[arg-type]
            [ChatMessage(role="user", content="帮我做一份成都三日游攻略")],
        )
    ]

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert "哪一天开始" in answer
    assert research.check_login_calls == 0
