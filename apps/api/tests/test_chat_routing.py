from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from app.core.settings import Settings
from app.schemas.chat import ChatMessage
from app.schemas.itinerary import (
    DayPlan,
    ExperienceValidation,
    ItineraryPlan,
    TripRequest,
    TripRequestExtraction,
)
from app.schemas.routing import TripRouteDecision
from app.schemas.tool_execution import MessageDeltaEvent, PlanningStageEvent
from app.services.chat_service import ChatService
from app.services.tool_execution import ToolExecutionContext
from app.services.trip_plan_service import StoredTripPlan
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


class FakePlanService:
    def __init__(self, current: StoredTripPlan | None = None) -> None:
        self.current = current
        self.saved = 0
        self.drafts = 0
        self.partials = 0
        self.saved_request: TripRequest | None = None
        self.saved_partial: ItineraryPlan | None = None

    async def get_current(self, _: uuid.UUID) -> StoredTripPlan | None:
        return self.current

    async def save_draft(
        self,
        _: uuid.UUID,
        request: TripRequest,
        *,
        title: str,
    ) -> uuid.UUID:
        assert title
        self.drafts += 1
        self.saved_request = request
        return uuid.uuid4()

    async def save_partial_plan(
        self,
        _: uuid.UUID,
        request: TripRequest,
        plan: ItineraryPlan,
    ) -> StoredTripPlan:
        self.partials += 1
        self.saved_request = request
        self.saved_partial = plan
        return StoredTripPlan(uuid.uuid4(), request, plan, "draft", 0)

    async def save_plan(
        self,
        _: uuid.UUID,
        request: TripRequest,
        plan: ItineraryPlan,
        *,
        change_summary: str | None = None,
    ) -> StoredTripPlan:
        del change_summary
        self.saved += 1
        self.saved_request = request
        return StoredTripPlan(uuid.uuid4(), request, plan, "active", self.saved)


def _settings() -> Settings:
    return Settings(
        app_name="test",
        model_config_path=Path("models.yaml"),
        cors_origins=(),
        log_level="WARNING",
    )


@pytest.mark.asyncio
async def test_chat_service_sends_complete_plan_to_langgraph_without_binding_tools() -> None:
    request = TripRequest(
        origin="南京",
        destinations=["杭州"],
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
    )
    plan = ItineraryPlan(
        title="杭州两日游",
        origin="南京",
        destination="杭州",
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
        days=[
            DayPlan(date=date(2026, 7, 20), day_index=1),
            DayPlan(date=date(2026, 7, 21), day_index=2),
        ],
    )
    model = FakeHybridModel(
        {
            "TripRequestExtraction": [TripRequestExtraction(request=request)],
            "ItineraryPlan": [plan],
            "ExperienceValidation": [ExperienceValidation()],
        }
    )
    plan_service = FakePlanService()
    registry = FakeRegistry(
        model,
        _router_model(
            TripRouteDecision(
                route="trip_planner",
                trip_action_hint="create",
                reason_code="create_trip",
            )
        ),
    )
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        trip_plan_service=plan_service,  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )

    events = [
        event
        async for event in service.stream(
            "test",
            [ChatMessage(role="user", content="帮我规划南京到杭州两日游")],
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        )
    ]

    assert any(isinstance(event, PlanningStageEvent) for event in events)
    assert plan_service.saved == 0
    assert plan_service.drafts == 0
    assert plan_service.partials == 1
    assert plan_service.saved_partial is not None
    assert model.bind_calls == 0
    assert registry.model_ids == ["test"]
    assert registry.router_calls == 1


@pytest.mark.asyncio
async def test_chat_service_continues_a_saved_draft_from_a_follow_up_message() -> None:
    conversation_id = uuid.uuid4()
    stored = StoredTripPlan(
        id=uuid.uuid4(),
        request=TripRequest(origin="Nanjing", destinations=["Hangzhou"]),
        plan=None,
        status="draft",
        version=0,
    )
    extracted_update = TripRequest(
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
    )
    completed_request = TripRequest(
        origin="Nanjing",
        destinations=["Hangzhou"],
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
    )
    plan = ItineraryPlan(
        title="Hangzhou two-day trip",
        origin="Nanjing",
        destination="Hangzhou",
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 21),
        days=[
            DayPlan(date=date(2026, 7, 20), day_index=1),
            DayPlan(date=date(2026, 7, 21), day_index=2),
        ],
    )
    model = FakeHybridModel(
        {
            "TripRequestExtraction": [TripRequestExtraction(request=extracted_update)],
            "ItineraryPlan": [plan],
            "ExperienceValidation": [ExperienceValidation()],
        }
    )
    plan_service = FakePlanService(stored)
    registry = FakeRegistry(
        model,
        _router_model(
            TripRouteDecision(
                route="trip_planner",
                trip_action_hint="none",
                reason_code="resume_draft",
            )
        ),
    )
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        trip_plan_service=plan_service,  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )

    events = [
        event
        async for event in service.stream(
            "test",
            [ChatMessage(role="user", content="July 20 to July 21")],
            execution_context=ToolExecutionContext(uuid.uuid4(), conversation_id),
        )
    ]

    assert any(isinstance(event, PlanningStageEvent) for event in events)
    assert plan_service.saved == 0
    assert plan_service.drafts == 0
    assert plan_service.partials == 1
    assert plan_service.saved_partial is not None
    assert plan_service.saved_request == completed_request
    assert model.bind_calls == 0


@pytest.mark.asyncio
async def test_chat_service_keeps_single_query_on_existing_agent_executor() -> None:
    model = FakeHybridModel({})
    registry = FakeRegistry(
        model,
        _router_model(
            TripRouteDecision(
                route="general_agent",
                reason_code="single_travel_query",
            )
        ),
    )
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        trip_plan_service=FakePlanService(),  # type: ignore[arg-type]
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
    assert registry.model_ids == ["test"]
    assert registry.router_calls == 1


@pytest.mark.asyncio
async def test_chat_service_outputs_route_clarification_without_running_an_agent() -> None:
    model = FakeHybridModel({})
    registry = FakeRegistry(
        model,
        _router_model(
            TripRouteDecision(
                route="clarify",
                clarification_kind="query_or_plan",
                reason_code="ambiguous_persistence",
            )
        ),
    )
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        trip_plan_service=FakePlanService(),  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )

    events = [
        event
        async for event in service.stream(
            "test",
            [ChatMessage(role="user", content="把刚才那个加进去")],
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        )
    ]

    assert [event.delta for event in events if isinstance(event, MessageDeltaEvent)] == [
        "你是只想查询相关信息，还是希望把结果加入行程？"
    ]
    assert model.bind_calls == 0


@pytest.mark.asyncio
async def test_chat_service_continues_sse_via_general_agent_when_router_fails() -> None:
    model = FakeHybridModel({}, answer="路由降级后仍可回答")
    registry = FakeRegistry(model, _router_model(RuntimeError("router failed")))
    service = ChatService(
        registry,  # type: ignore[arg-type]
        [],
        trip_plan_service=FakePlanService(),  # type: ignore[arg-type]
        trip_planner_settings=_settings(),
    )

    events = [
        event
        async for event in service.stream(
            "user-selected-model",
            [ChatMessage(role="user", content="帮我规划成都四日游")],
            execution_context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
        )
    ]

    assert (
        "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
        == "路由降级后仍可回答"
    )
    assert model.bind_calls == 1
    assert registry.model_ids == ["user-selected-model"]
    assert registry.router_calls == 1
