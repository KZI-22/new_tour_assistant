from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.settings import Settings
from app.schemas.chat import ChatMessage
from app.schemas.routing import TripRouteDecision
from app.schemas.tool_execution import MessageDeltaEvent, PlanningStageEvent
from app.schemas.xhs_planning import XhsPostEvidence, XhsResearchResult
from app.services.chat_service import ChatService
from langchain_core.messages import AIMessage


class _Runnable:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def ainvoke(self, _: Any) -> Any:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeHybridModel:
    def __init__(self, structured: dict[str, list[Any]], answer: str = "单项查询") -> None:
        self.structured = defaultdict(list, structured)
        self.answer = answer
        self.bind_calls = 0

    def with_structured_output(self, schema: type[Any]) -> _Runnable:
        return _Runnable(self.structured[schema.__name__].pop(0))

    def bind_tools(self, _: Any) -> FakeHybridModel:
        self.bind_calls += 1
        return self

    async def ainvoke(self, _: Any) -> AIMessage:
        return AIMessage(content=self.answer)


class FakeRegistry:
    def __init__(
        self,
        model: FakeHybridModel,
        router_model: FakeHybridModel,
    ) -> None:
        self.model = model
        self.router_model = router_model
        self.model_ids: list[str] = []
        self.router_calls = 0

    def create_model(self, model_id: str) -> FakeHybridModel:
        self.model_ids.append(model_id)
        return self.model

    def create_router_model(self) -> tuple[FakeHybridModel, float]:
        self.router_calls += 1
        return self.router_model, 1


def _router_model(decision: TripRouteDecision | Exception) -> FakeHybridModel:
    return FakeHybridModel({"TripRouteDecision": [decision]})


class FakeResearchService:
    def __init__(self) -> None:
        self.keywords: list[str] = []
        self.login_checks = 0

    async def check_login(self) -> SimpleNamespace:
        self.login_checks += 1
        return SimpleNamespace(is_logged_in=True)

    async def collect(
        self,
        keyword: str,
        *,
        on_search_complete: Any = None,
        on_trace: Any = None,
    ) -> XhsResearchResult:
        del on_trace
        self.keywords.append(keyword)
        if on_search_complete is not None:
            on_search_complete(2)
        return XhsResearchResult(
            keyword=keyword,
            posts=[
                XhsPostEvidence(
                    reference_id="source_1",
                    role="primary",
                    note_id="note-1",
                    search_rank=1,
                    title="成都三日攻略",
                    author_name="作者甲",
                    published_at="2026-07-01T12:00:00+08:00",
                    content="第一天宽窄巷子，第二天熊猫基地，第三天人民公园。",
                    liked_count_raw="3万+",
                    liked_count=30_000,
                    queried_at=datetime.now(UTC),
                ),
                XhsPostEvidence(
                    reference_id="source_2",
                    role="supplementary",
                    note_id="note-2",
                    search_rank=2,
                    title="成都美食路线",
                    author_name="作者乙",
                    content="建议体验本地小吃并合理安排每天的片区。",
                    liked_count_raw="1.2万",
                    liked_count=12_000,
                    queried_at=datetime.now(UTC),
                ),
            ],
        )


def _settings() -> Settings:
    return Settings(
        app_name="test",
        model_config_path=Path("models.yaml"),
        cors_origins=(),
        log_level="WARNING",
    )


def _service() -> tuple[ChatService, FakeRegistry, FakeHybridModel, FakeResearchService]:
    model = FakeHybridModel({})
    registry = FakeRegistry(
        model,
        _router_model(TripRouteDecision(route="trip_planner")),
    )
    research = FakeResearchService()
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        xhs_research_service=research,  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )
    return service, registry, model, research


@pytest.mark.asyncio
async def test_explicit_xhs_mode_bypasses_all_llm_calls_and_returns_raw_posts() -> None:
    service, registry, model, research = _service()

    events = [
        event
        async for event in service.stream(
            "model-that-must-not-be-created",
            [ChatMessage(role="user", content="帮我规划成都三日美食之旅")],
            planning_source="xhs",
        )
    ]

    stages = [event.stage for event in events if isinstance(event, PlanningStageEvent)]
    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert research.keywords == ["帮我规划成都三日美食之旅"]
    assert "searching_xhs" in stages
    assert "reading_xhs_posts" in stages
    assert "第一天宽窄巷子，第二天熊猫基地，第三天人民公园。" in answer
    assert "建议体验本地小吃并合理安排每天的片区。" in answer
    assert "未经过 LLM 改写" in answer
    assert registry.model_ids == []
    assert registry.router_calls == 0
    assert model.bind_calls == 0


@pytest.mark.asyncio
async def test_xhs_mode_does_not_require_duration_or_start_date() -> None:
    service, registry, _, research = _service()

    events = [
        event
        async for event in service.stream(
            "unused",
            [ChatMessage(role="user", content="  杭州\n旅行  ")],
            planning_source="xhs",
        )
    ]

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert research.keywords == ["杭州 旅行"]
    assert "小红书原帖检索结果" in answer
    assert registry.router_calls == 0


@pytest.mark.asyncio
async def test_xhs_mode_is_explicit_even_for_a_non_planning_query() -> None:
    service, registry, model, research = _service()

    events = [
        event
        async for event in service.stream(
            "unused",
            [ChatMessage(role="user", content="帮我查明天南京到杭州的高铁")],
            planning_source="xhs",
        )
    ]

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert research.keywords == ["帮我查明天南京到杭州的高铁"]
    assert "单项查询" not in answer
    assert registry.model_ids == []
    assert registry.router_calls == 0
    assert model.bind_calls == 0


@pytest.mark.asyncio
async def test_standard_general_query_keeps_existing_agent_executor() -> None:
    model = FakeHybridModel({})
    registry = FakeRegistry(
        model,
        _router_model(TripRouteDecision(route="general_agent")),
    )
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        xhs_research_service=FakeResearchService(),  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )

    events = [
        event
        async for event in service.stream(
            "test",
            [ChatMessage(role="user", content="帮我查明天南京到杭州的高铁")],
        )
    ]

    assert (
        "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
        == "单项查询"
    )
    assert registry.model_ids == ["test"]
    assert registry.router_calls == 1
    assert model.bind_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "帮我查 2026-07-25 南京到成都的航班",
        "推荐几个 2026-07-25 入住、7 月 26 日退房的成都酒店",
        "你好，介绍一下成都",
    ],
)
async def test_non_itinerary_requests_stay_on_general_agent(
    message: str,
) -> None:
    model = FakeHybridModel({})
    registry = FakeRegistry(
        model,
        _router_model(TripRouteDecision(route="general_agent")),
    )
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        xhs_research_service=FakeResearchService(),  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )

    events = [
        event
        async for event in service.stream(
            "test",
            [ChatMessage(role="user", content=message)],
        )
    ]

    assert (
        "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
        == "单项查询"
    )
    assert model.bind_calls == 1


@pytest.mark.asyncio
async def test_router_failure_still_falls_back_to_general_agent() -> None:
    model = FakeHybridModel({})
    registry = FakeRegistry(
        model,
        _router_model(RuntimeError("router unavailable")),
    )
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        xhs_research_service=FakeResearchService(),  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )

    events = [
        event
        async for event in service.stream(
            "test",
            [ChatMessage(role="user", content="帮我规划成都三日游")],
        )
    ]

    assert (
        "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
        == "单项查询"
    )
    assert model.bind_calls == 1


@pytest.mark.asyncio
async def test_standard_source_routes_planning_intent_to_trip_planner_graph() -> None:
    service, registry, _, research = _service()

    class FakeStandardTripPlanner:
        calls = 0

        async def stream(
            self,
            _: object,
            messages: list[ChatMessage],
            *,
            route_source: str,
            execution_context: object | None,
        ) -> Any:
            self.calls += 1
            assert messages[-1].content == "帮我规划成都三日游"
            assert route_source == "llm_router"
            assert execution_context is None
            yield MessageDeltaEvent(delta="地图方案")

    fake_planner = FakeStandardTripPlanner()
    service._standard_trip_planner = fake_planner  # type: ignore[assignment]

    events = [
        event
        async for event in service.stream(
            "test",
            [ChatMessage(role="user", content="帮我规划成都三日游")],
        )
    ]

    assert [event.delta for event in events if isinstance(event, MessageDeltaEvent)] == ["地图方案"]
    assert fake_planner.calls == 1
    assert registry.router_calls == 1
    assert research.keywords == []
    assert research.login_checks == 0
