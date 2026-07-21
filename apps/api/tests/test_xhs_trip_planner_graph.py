from __future__ import annotations

import asyncio
import json
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.settings import Settings
from app.graphs.xhs_trip_planner import (
    XhsTripPlanner,
    XhsTripPlanningError,
    build_search_keyword,
)
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import (
    MessageDeltaEvent,
    PlanningStageEvent,
    PlanningTraceEvent,
)
from app.schemas.xhs_planning import XhsPostEvidence, XhsResearchResult
from app.services.xhs_posts_renderer import render_xhs_posts
from app.services.xhs_research_service import XhsResearchError, XhsResearchTraceUpdate


def _settings() -> Settings:
    return Settings(
        app_name="test",
        model_config_path=Path("models.yaml"),
        cors_origins=(),
        log_level="WARNING",
        xhs_login_poll_seconds=0.01,
        xhs_sse_heartbeat_seconds=0.01,
    )


def _research(keyword: str = "成都三日游") -> XhsResearchResult:
    return XhsResearchResult(
        keyword=keyword,
        posts=[
            XhsPostEvidence(
                reference_id="source_1",
                role="primary",
                note_id="fixture-note-1",
                search_rank=1,
                title="脱敏笔记一",
                author_name="脱敏作者甲",
                published_at="2026-07-01T12:00:00+08:00",
                content="原帖第一段。\n原帖第二段，保留 #话题 和 emoji 🐼。",
                liked_count_raw="3万+",
                liked_count=30_000,
                queried_at=datetime.now(UTC),
            ),
            XhsPostEvidence(
                reference_id="source_2",
                role="supplementary",
                note_id="fixture-note-2",
                search_rank=2,
                title="脱敏笔记二",
                author_name="脱敏作者乙",
                content="第二篇原帖正文。",
                liked_count_raw="1.2万",
                liked_count=12_000,
                queried_at=datetime.now(UTC),
            ),
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
        self.keywords: list[str] = []

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
        keyword: str,
        *,
        on_search_complete: Any = None,
        on_trace: Any = None,
    ) -> XhsResearchResult:
        self.collect_calls += 1
        self.keywords.append(keyword)
        if on_search_complete is not None:
            on_search_complete(2)
        if on_trace is not None:
            on_trace(
                XhsResearchTraceUpdate(
                    step="search_results",
                    title="小红书返回 2 条搜索结果",
                    status="success",
                    data={
                        "keyword": keyword,
                        "total_count": 2,
                        "candidate_count": 2,
                        "posts": [
                            {
                                "search_rank": 1,
                                "note_id": "fixture-note-1",
                                "title": "脱敏笔记一",
                                "author_name": "脱敏作者甲",
                                "liked_count_raw": "3万+",
                                "selection_status": "candidate",
                            }
                        ],
                    },
                )
            )
            on_trace(
                XhsResearchTraceUpdate(
                    step="evidence_selected",
                    title="最终采用 2 篇小红书笔记",
                    status="success",
                    data={
                        "posts": [
                            {
                                "reference_id": "source_1",
                                "note_id": "fixture-note-1",
                                "title": "脱敏笔记一",
                            }
                        ]
                    },
                )
            )
        result = self._collections.popleft()
        if isinstance(result, Exception):
            raise result
        return result.model_copy(update={"keyword": keyword})


async def _collect_events(
    research: FakeResearchService,
    message: str = "成都三日游",
) -> list[Any]:
    planner = XhsTripPlanner(research, _settings())  # type: ignore[arg-type]
    return [
        event
        async for event in planner.stream(
            [ChatMessage(role="user", content=message)],
        )
    ]


def test_build_search_keyword_normalizes_whitespace_and_caps_mcp_input() -> None:
    assert build_search_keyword("  成都\n三日游  ") == "成都 三日游"
    assert build_search_keyword("a" * 250) == "a" * 200


@pytest.mark.asyncio
async def test_logged_in_request_searches_latest_message_and_returns_raw_body() -> None:
    research = FakeResearchService(login_checks=[True])

    events = await _collect_events(research, "  成都\n三日游  ")

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    stages = [event.stage for event in events if isinstance(event, PlanningStageEvent)]
    assert research.keywords == ["成都 三日游"]
    assert research.check_calls == 1
    assert research.collect_calls == 1
    assert "原帖第一段。\n原帖第二段，保留 #话题 和 emoji 🐼。" in answer
    assert "第二篇原帖正文。" in answer
    assert "未经过 LLM 改写" in answer
    assert "generating_itinerary" not in stages
    assert "collecting_weather" not in stages


@pytest.mark.asyncio
async def test_planner_emits_ordered_sanitized_no_llm_trace() -> None:
    events = await _collect_events(FakeResearchService(login_checks=[True]))

    traces = [event for event in events if isinstance(event, PlanningTraceEvent)]
    assert [trace.sequence for trace in traces] == list(range(1, len(traces) + 1))
    assert [trace.step for trace in traces[:3]] == [
        "request_received",
        "route_selected",
        "search_query_built",
    ]
    assert traces[1].data == {
        "route": "xhs_post_search",
        "route_source": "explicit",
        "llm_used": False,
    }
    assert traces[-1].data["llm_used"] is False
    serialized = json.dumps([trace.model_dump(mode="json") for trace in traces])
    assert "xsec_token" not in serialized
    assert "fixture-login" not in serialized


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
    assert login_events[0].fallback_available is True
    assert login_events[0].fallback_mode == "map_weather"
    assert research.status_calls == 3
    assert research.collect_calls == 1


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
async def test_terminal_login_status_stops_search(status: str, code: str) -> None:
    research = FakeResearchService(login_checks=[False], statuses=[status])

    with pytest.raises(XhsTripPlanningError) as raised:
        await _collect_events(research)

    assert raised.value.code == code
    assert research.collect_calls == 0


@pytest.mark.asyncio
async def test_cancelling_login_wait_calls_remote_cancel() -> None:
    research = FakeResearchService(login_checks=[False])
    planner = XhsTripPlanner(research, _settings())  # type: ignore[arg-type]
    stream = planner.stream([ChatMessage(role="user", content="成都三日游")])
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


@pytest.mark.asyncio
async def test_empty_user_message_fails_before_login() -> None:
    research = FakeResearchService(login_checks=[True])

    with pytest.raises(XhsTripPlanningError) as raised:
        await _collect_events(research, "  \n  ")

    assert raised.value.code == "XHS_SEARCH_KEYWORD_MISSING"
    assert research.check_calls == 0


def test_renderer_keeps_post_body_unchanged() -> None:
    research = _research()

    rendered = render_xhs_posts(research)

    for post in research.posts:
        assert post.content in rendered
    assert "《脱敏笔记一》" in rendered
    assert "点赞：3万+" in rendered
    assert "笔记 ID：fixture-note-1" in rendered
