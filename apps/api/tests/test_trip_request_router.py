from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date
from typing import Any

import pytest
from app.schemas.chat import ChatMessage
from app.schemas.itinerary import TripRequest
from app.schemas.routing import TripRouteDecision
from app.services.trip_plan_service import StoredTripPlan
from app.services.trip_request_router import (
    TripRequestRouter,
    build_route_context,
    resolve_planning_intent,
)
from pydantic import ValidationError


class _Runnable:
    def __init__(
        self,
        value: Any,
        captured_messages: list[Any],
        *,
        delay: float = 0,
    ) -> None:
        self.value = value
        self.captured_messages = captured_messages
        self.delay = delay

    async def ainvoke(self, messages: list[Any]) -> Any:
        self.captured_messages.extend(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeRouterModel:
    def __init__(
        self,
        value: Any,
        *,
        delay: float = 0,
        structured_error: Exception | None = None,
    ) -> None:
        self.value = value
        self.delay = delay
        self.structured_error = structured_error
        self.schema: type[Any] | None = None
        self.captured_messages: list[Any] = []

    def with_structured_output(self, schema: type[Any]) -> _Runnable:
        self.schema = schema
        if self.structured_error is not None:
            raise self.structured_error
        return _Runnable(
            self.value,
            self.captured_messages,
            delay=self.delay,
        )


class FakeRegistry:
    def __init__(
        self,
        model: FakeRouterModel | None,
        *,
        timeout_seconds: float = 1,
        factory_error: Exception | None = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.factory_error = factory_error
        self.create_calls = 0

    def create_router_model(self) -> tuple[FakeRouterModel, float]:
        self.create_calls += 1
        if self.factory_error is not None:
            raise self.factory_error
        assert self.model is not None
        return self.model, self.timeout_seconds


@pytest.mark.asyncio
async def test_router_uses_recent_context_and_draft_summary() -> None:
    messages = [
        ChatMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=f"message-{index}",
        )
        for index in range(10)
    ]
    stored = StoredTripPlan(
        id=uuid.uuid4(),
        request=TripRequest(
            origin="南京",
            destinations=["杭州"],
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 23),
        ),
        plan=None,
        status="draft",
        version=0,
    )
    model = FakeRouterModel(
        TripRouteDecision(
            route="trip_planner",
            trip_action_hint="create",
            reason_code="resume_draft",
        )
    )
    registry = FakeRegistry(model)

    result = await TripRequestRouter(registry).route(messages, stored=stored)  # type: ignore[arg-type]

    assert result.route == "trip_planner"
    assert result.source == "llm_router"
    assert registry.create_calls == 1
    assert model.schema is TripRouteDecision
    assert len(model.captured_messages) == 2
    context_content = model.captured_messages[1].content
    assert isinstance(context_content, str)
    context = json.loads(context_content)
    assert [item["content"] for item in context["recent_messages"]] == [
        f"message-{index}" for index in range(2, 10)
    ]
    assert context["latest_user_message"] == "message-8"
    assert context["has_current_plan"] is False
    assert context["has_draft"] is True
    assert context["stored_plan_status"] == "draft"
    assert context["current_plan_summary"] == {
        "destination": "杭州",
        "start_date": "2026-07-20",
        "end_date": "2026-07-23",
        "has_formal_plan": False,
    }


def test_route_context_excludes_system_messages_and_limits_message_size() -> None:
    context = build_route_context(
        [
            ChatMessage(role="system", content="internal prompt"),
            ChatMessage(role="user", content=" x " * 5_000),
        ],
        stored=None,
    )

    assert len(context.recent_messages) == 1
    assert context.recent_messages[0].role == "user"
    assert len(context.recent_messages[0].content) == 4_000
    assert context.current_plan_summary is None


@pytest.mark.parametrize(
    "invalid",
    [
        {
            "route": "general_agent",
            "trip_action_hint": "create",
            "reason_code": "general_conversation",
        },
        {
            "route": "trip_planner",
            "clarification_kind": "query_or_plan",
            "reason_code": "create_trip",
        },
        {
            "route": "clarify",
            "clarification_kind": "none",
            "reason_code": "ambiguous_persistence",
        },
        {
            "route": "general_agent",
            "reason_code": "create_trip",
        },
    ],
)
def test_route_decision_rejects_invalid_field_combinations(
    invalid: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        TripRouteDecision.model_validate(invalid)


@pytest.mark.parametrize(
    ("hint", "has_plan", "has_draft", "reason", "expected"),
    [
        ("create", False, False, "create_trip", "new_trip_plan"),
        ("modify", True, False, "modify_trip", "modify_trip_plan"),
        ("modify", False, False, "modify_trip", "new_trip_plan"),
        ("none", True, False, "modify_trip", "modify_trip_plan"),
        ("none", False, True, "resume_draft", "new_trip_plan"),
    ],
)
def test_planner_compatibility_mapping(
    hint: Any,
    has_plan: bool,
    has_draft: bool,
    reason: Any,
    expected: str,
) -> None:
    route = TripRouteDecision(
        route="trip_planner",
        trip_action_hint=hint,
        reason_code=reason,
    )

    assert (
        resolve_planning_intent(
            route,
            has_current_plan=has_plan,
            has_draft=has_draft,
        )
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "registry",
    [
        FakeRegistry(None, factory_error=RuntimeError("not configured")),
        FakeRegistry(
            FakeRouterModel(
                None,
                structured_error=NotImplementedError("structured output unavailable"),
            )
        ),
        FakeRegistry(FakeRouterModel({"route": "invalid"})),
        FakeRegistry(FakeRouterModel(RuntimeError("provider failed"))),
        FakeRegistry(
            FakeRouterModel(
                TripRouteDecision(
                    route="general_agent",
                    reason_code="general_conversation",
                ),
                delay=0.02,
            ),
            timeout_seconds=0.001,
        ),
    ],
)
async def test_router_failures_fall_back_to_general_agent(
    registry: FakeRegistry,
) -> None:
    result = await TripRequestRouter(registry).route(  # type: ignore[arg-type]
        [ChatMessage(role="user", content="帮我规划成都四日游")],
        stored=None,
    )

    assert result.route == "general_agent"
    assert result.source == "fallback"
    assert result.trip_action_hint == "none"
    assert result.clarification_kind == "none"
