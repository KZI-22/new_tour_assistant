from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.settings import Settings
from app.graphs.xhs_trip_planner import (
    _GENERATION_SYSTEM_PROMPT,
    XhsTripPlanner,
    XhsTripPlanningError,
    _generation_prompt,
    _latest_explicit_duration_days,
    build_search_keyword,
)
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import MessageDeltaEvent, PlanningStageEvent
from app.schemas.xhs_planning import (
    XhsDayPlan,
    XhsItineraryPlan,
    XhsPlanActivity,
    XhsPlanSource,
    XhsPostEvidence,
    XhsResearchResult,
    XhsTripRequest,
    XhsTripRequestExtraction,
)
from app.services.xhs_itinerary_renderer import render_xhs_itinerary
from app.services.xhs_research_service import XhsResearchError


def test_build_search_keyword_uses_only_city_and_duration() -> None:
    assert build_search_keyword(" 成都 ", 3) == "成都 3日游 攻略"


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


class _Runnable:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def ainvoke(self, _: Any) -> Any:
        return self.value


class FakeModel:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.responses = defaultdict(list, responses)

    def with_structured_output(self, schema: type[Any]) -> _Runnable:
        return _Runnable(self.responses[schema.__name__].pop(0))


def _settings() -> Settings:
    return Settings(
        app_name="test",
        model_config_path=Path("models.yaml"),
        cors_origins=(),
        log_level="WARNING",
        xhs_login_poll_seconds=0.01,
        xhs_sse_heartbeat_seconds=0.01,
    )


def _plan() -> XhsItineraryPlan:
    return XhsItineraryPlan(
        title="成都三日攻略",
        destination_city="成都",
        duration_days=3,
        summary="脱敏测试攻略",
        days=[
            XhsDayPlan(
                day_index=index,
                theme=f"第 {index} 天",
                activities=[
                    XhsPlanActivity(
                        time_of_day="morning",
                        place_name=f"测试地点 {index}",
                        description="脱敏活动说明",
                        source_refs=["source_1"],
                    )
                ],
            )
            for index in range(1, 4)
        ],
    )


def _research() -> XhsResearchResult:
    return XhsResearchResult(
        keyword="成都 3日游 攻略",
        posts=[
            XhsPostEvidence(
                reference_id="source_1",
                role="primary",
                note_id="fixture-note",
                search_rank=1,
                title="脱敏笔记",
                author_name="脱敏作者",
                content="脱敏测试正文。" * 40,
                liked_count_raw="3万+",
                liked_count=30_000,
                queried_at=datetime.now(UTC),
            )
        ],
    )


def _session(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        login_id="fixture-login",
        status=status,
        created_at="2026-07-19T10:00:00+08:00",
        expires_at="2026-07-19T10:05:00+08:00",
        is_logged_in=status == "succeeded",
        message=(
            "请在已打开的 Google Chrome 中完成手机号、验证码或其他安全验证。"
            if status == "pending"
            else f"fixture {status}"
        ),
    )


class FakeResearchService:
    def __init__(
        self,
        *,
        login_checks: list[bool],
        start_status: str = "pending",
        statuses: list[str] | None = None,
        collections: list[XhsResearchResult | Exception] | None = None,
    ) -> None:
        self._login_checks = deque(login_checks)
        self._start_status = start_status
        self._statuses = deque(statuses or [])
        self._collections = deque(collections or [_research()])
        self.check_calls = 0
        self.start_calls = 0
        self.status_calls = 0
        self.cancel_calls: list[str] = []
        self.collect_calls = 0

    async def check_login(self) -> SimpleNamespace:
        self.check_calls += 1
        return SimpleNamespace(is_logged_in=self._login_checks.popleft())

    async def start_login(self) -> SimpleNamespace:
        self.start_calls += 1
        return _session(self._start_status)

    async def get_login_status(self, login_id: str) -> SimpleNamespace:
        assert login_id == "fixture-login"
        self.status_calls += 1
        status = self._statuses.popleft() if self._statuses else "pending"
        return _session(status)

    async def cancel_login(self, login_id: str) -> SimpleNamespace:
        self.cancel_calls.append(login_id)
        return _session("cancelled")

    async def collect(
        self,
        _: str,
        *,
        on_search_complete: Any = None,
    ) -> XhsResearchResult:
        self.collect_calls += 1
        if on_search_complete is not None:
            on_search_complete(1)
        result = self._collections.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def _planner(research: FakeResearchService) -> tuple[XhsTripPlanner, FakeModel]:
    model = FakeModel(
        {
            "XhsTripRequestExtraction": [
                XhsTripRequestExtraction(
                    request=XhsTripRequest(destination_city="成都", duration_days=3)
                )
            ],
            "XhsItineraryPlan": [_plan()],
        }
    )
    return XhsTripPlanner(research, _settings()), model  # type: ignore[arg-type]


async def _collect_events(research: FakeResearchService) -> list[Any]:
    planner, model = _planner(research)
    return [
        event
        async for event in planner.stream(  # type: ignore[arg-type]
            model,
            [ChatMessage(role="user", content="帮我做成都三日游攻略")],
        )
    ]


@pytest.mark.asyncio
async def test_logged_in_request_searches_without_login_event() -> None:
    research = FakeResearchService(login_checks=[True])

    events = await _collect_events(research)

    assert research.check_calls == 1
    assert research.start_calls == 0
    assert research.status_calls == 0
    assert research.collect_calls == 1
    assert not any(getattr(event, "type", None) == "xhs_login_required" for event in events)
    stages = [event.stage for event in events if isinstance(event, PlanningStageEvent)]
    assert "checking_xhs_login" in stages


@pytest.mark.asyncio
async def test_pending_login_prompts_for_chrome_and_continues_after_success() -> None:
    research = FakeResearchService(
        login_checks=[False],
        statuses=["pending", "pending", "succeeded"],
    )

    events = await _collect_events(research)

    login_events = [event for event in events if event.type == "xhs_login_required"]
    assert len(login_events) == 1
    assert login_events[0].login_id == "fixture-login"
    assert "Google Chrome" in login_events[0].message
    assert "验证码" in login_events[0].message
    assert research.status_calls == 3
    assert research.collect_calls == 1
    assert all(not isinstance(event, MessageDeltaEvent) for event in login_events)


@pytest.mark.asyncio
async def test_already_succeeded_start_skips_login_event_and_status_polling() -> None:
    research = FakeResearchService(login_checks=[False], start_status="succeeded")

    events = await _collect_events(research)

    assert not any(getattr(event, "type", None) == "xhs_login_required" for event in events)
    assert research.start_calls == 1
    assert research.status_calls == 0
    assert research.collect_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("expired", "XHS_LOGIN_EXPIRED"),
        ("cancelled", "XHS_LOGIN_CANCELLED"),
        ("failed", "XHS_LOGIN_FAILED"),
    ],
)
async def test_terminal_login_status_stops_planning(status: str, code: str) -> None:
    research = FakeResearchService(login_checks=[False], statuses=[status])

    with pytest.raises(XhsTripPlanningError) as raised:
        await _collect_events(research)

    assert raised.value.code == code
    assert research.collect_calls == 0


@pytest.mark.asyncio
async def test_cancelling_login_wait_calls_remote_cancel() -> None:
    research = FakeResearchService(login_checks=[False])
    planner, model = _planner(research)
    stream = planner.stream(  # type: ignore[arg-type]
        model,
        [ChatMessage(role="user", content="帮我做成都三日游攻略")],
    )
    while True:
        event = await anext(stream)
        if getattr(event, "type", None) == "xhs_login_required":
            break

    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.02)
    pending.cancel()
    with suppress(asyncio.CancelledError):
        await pending
    await stream.aclose()

    assert research.cancel_calls == ["fixture-login"]


@pytest.mark.asyncio
async def test_not_logged_in_search_error_recovers_once_then_retries() -> None:
    research = FakeResearchService(
        login_checks=[True, False],
        start_status="succeeded",
        collections=[
            XhsResearchError("NOT_LOGGED_IN", "需要重新登录。"),
            _research(),
        ],
    )

    await _collect_events(research)

    assert research.check_calls == 2
    assert research.start_calls == 1
    assert research.collect_calls == 2


def test_generation_prompt_encodes_primary_and_supplementary_roles() -> None:
    primary = _research().posts[0]
    supplementary = primary.model_copy(
        update={
            "reference_id": "source_2",
            "role": "supplementary",
            "note_id": "fixture-note-2",
            "title": "脱敏补充笔记",
            "liked_count_raw": "1.2万",
            "liked_count": 12_000,
            "content": "只用于补充的脱敏正文。" * 30,
        }
    )
    research = XhsResearchResult(
        keyword="成都 3日游 攻略",
        posts=[primary, supplementary],
    )

    prompt = json.loads(
        _generation_prompt(
            [ChatMessage(role="user", content="路线紧凑一点")],
            XhsTripRequest(destination_city="成都", duration_days=3),
            research,
        )
    )

    assert prompt["search"] == {
        "keyword": "成都 3日游 攻略",
        "sort_by": "most_liked",
        "result_scope": "initial_results_only",
    }
    assert [(source["reference_id"], source["role"]) for source in prompt["sources"]] == [
        ("source_1", "primary"),
        ("source_2", "supplementary"),
    ]
    assert prompt["requirements"]["primary_source"] == "source_1"
    assert "主笔记" in _GENERATION_SYSTEM_PROMPT
    assert "补充笔记" in _GENERATION_SYSTEM_PROMPT
    assert primary.content not in _GENERATION_SYSTEM_PROMPT


def test_single_post_generation_prompt_does_not_invent_source_2() -> None:
    prompt = json.loads(
        _generation_prompt(
            [],
            XhsTripRequest(destination_city="成都", duration_days=3),
            _research(),
        )
    )

    assert [source["reference_id"] for source in prompt["sources"]] == ["source_1"]
    assert prompt["requirements"]["allowed_source_refs"] == ["source_1"]
    assert "source_2" not in json.dumps(prompt, ensure_ascii=False)


@pytest.mark.asyncio
async def test_single_post_plan_rejects_activity_that_only_cites_source_2() -> None:
    research = FakeResearchService(login_checks=[True])
    invalid_plan = _plan()
    invalid_plan.days[0].activities[0].source_refs = ["source_2"]
    planner, model = _planner(research)
    model.responses["XhsItineraryPlan"] = [invalid_plan]

    with pytest.raises(XhsTripPlanningError) as raised:
        await _collect_events_with(planner, model)

    assert raised.value.code == "XHS_PLAN_SOURCE_INVALID"


async def _collect_events_with(planner: XhsTripPlanner, model: FakeModel) -> list[Any]:
    return [
        event
        async for event in planner.stream(  # type: ignore[arg-type]
            model,
            [ChatMessage(role="user", content="帮我做成都三日游攻略")],
        )
    ]


def test_renderer_displays_roles_likes_and_initial_result_scope() -> None:
    plan = _plan()
    plan.sources = [
        XhsPlanSource(
            reference_id="source_1",
            role="primary",
            note_id="fixture-note",
            title="脱敏主帖",
            author_name="脱敏作者",
            liked_count=52_000,
        ),
        XhsPlanSource(
            reference_id="source_2",
            role="supplementary",
            note_id="fixture-note-2",
            title="脱敏补充帖",
            author_name="脱敏作者乙",
            liked_count=38_000,
        ),
    ]

    rendered = render_xhs_itinerary(plan)

    assert "[主帖]《脱敏主帖》— 脱敏作者，点赞 5.2 万" in rendered
    assert "[补充]《脱敏补充帖》— 脱敏作者乙，点赞 3.8 万" in rendered
    assert "搜索页首次加载结果" in rendered
    assert "不代表平台全部内容" in rendered
