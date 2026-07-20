from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.settings import Settings
from app.schemas.chat import ChatMessage
from app.schemas.routing import TripRouteDecision
from app.schemas.tool_execution import MessageDeltaEvent, PlanningStageEvent
from app.schemas.xhs_planning import (
    XhsDayPlan,
    XhsItineraryPlan,
    XhsPlanActivity,
    XhsPostEvidence,
    XhsResearchResult,
    XhsTripRequest,
    XhsTripRequestExtraction,
)
from app.services.chat_service import ChatService
from app.services.tool_execution import ToolExecutionContext
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


def _plan() -> XhsItineraryPlan:
    return XhsItineraryPlan(
        title="成都三日小红书攻略",
        destination_city="成都",
        duration_days=3,
        summary="按片区安排三天行程。",
        days=[
            XhsDayPlan(
                day_index=index,
                theme=f"第 {index} 天主题",
                activities=[
                    XhsPlanActivity(
                        time_of_day="morning",
                        place_name=f"地点 {index}",
                        description="根据笔记安排。",
                        source_refs=["source_1"],
                    )
                ],
            )
            for index in range(1, 4)
        ],
    )


@pytest.mark.asyncio
async def test_chat_service_routes_city_plan_to_new_xhs_graph() -> None:
    model = FakeHybridModel(
        {
            "XhsTripRequestExtraction": [
                XhsTripRequestExtraction(
                    request=XhsTripRequest(
                        destination_city="成都",
                        duration_days=3,
                        start_date=date(2026, 7, 25),
                    )
                )
            ],
            "XhsItineraryPlan": [_plan()],
        }
    )
    research = FakeResearchService()
    registry = FakeRegistry(
        model,
        _router_model(
            TripRouteDecision(
                route="xhs_trip_planner",
            )
        ),
    )
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        xhs_research_service=research,  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )

    events = [
        event
        async for event in service.stream(
            "test",
            [ChatMessage(role="user", content="帮我规划成都三日美食之旅")],
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        )
    ]

    stages = [event.stage for event in events if isinstance(event, PlanningStageEvent)]
    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert research.keywords == ["成都 3日游 攻略"]
    assert "searching_xhs" in stages
    assert "reading_xhs_posts" in stages
    assert "成都三日小红书攻略" in answer
    assert "《成都三日攻略》" in answer
    assert "[主帖]" in answer
    assert "点赞 3 万" in answer
    assert "不代表平台全部内容" in answer
    assert "未查询机票、火车票、酒店库存或实时价格" in answer
    assert model.bind_calls == 0
    assert research.login_checks == 1


@pytest.mark.asyncio
async def test_xhs_graph_only_asks_for_missing_duration() -> None:
    model = FakeHybridModel(
        {
            "XhsTripRequestExtraction": [
                XhsTripRequestExtraction(
                    request=XhsTripRequest(
                        destination_city="杭州",
                        duration_days=None,
                        start_date=date(2026, 7, 25),
                    )
                )
            ]
        }
    )
    research = FakeResearchService()
    registry = FakeRegistry(
        model,
        _router_model(
            TripRouteDecision(
                route="xhs_trip_planner",
            )
        ),
    )
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        xhs_research_service=research,  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )

    events = [
        event
        async for event in service.stream(
            "test",
            [ChatMessage(role="user", content="帮我规划杭州旅行")],
        )
    ]

    assert [event.delta for event in events if isinstance(event, MessageDeltaEvent)] == [
        "请告诉我准备游玩几天。"
    ]
    assert research.keywords == []
    assert research.login_checks == 0


@pytest.mark.asyncio
async def test_chat_service_keeps_single_query_on_existing_agent_executor() -> None:
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
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        )
    ]

    assert (
        "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
        == "单项查询"
    )
    assert model.bind_calls == 1


@pytest.mark.asyncio
async def test_mixed_request_runs_xhs_planner_without_live_hotel_query() -> None:
    model = FakeHybridModel(
        {
            "XhsTripRequestExtraction": [
                XhsTripRequestExtraction(
                    request=XhsTripRequest(
                        destination_city="成都",
                        duration_days=3,
                        start_date=date(2026, 7, 25),
                    )
                )
            ],
            "XhsItineraryPlan": [_plan()],
        }
    )
    registry = FakeRegistry(
        model,
        _router_model(
            TripRouteDecision(
                route="xhs_trip_planner",
            )
        ),
    )
    research = FakeResearchService()
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        xhs_research_service=research,  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )

    events = [
        event
        async for event in service.stream(
            "test",
            [ChatMessage(role="user", content="规划成都三天并查一下机票")],
        )
    ]

    answer = "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
    assert research.keywords == ["成都 3日游 攻略"]
    assert "成都三日小红书攻略" in answer
    assert "未查询机票、火车票、酒店库存或实时价格" in answer
    assert model.bind_calls == 0
