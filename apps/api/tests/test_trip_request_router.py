from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from app.schemas.chat import ChatMessage
from app.schemas.routing import TripRouteDecision
from app.services.trip_request_router import TripRequestRouter, build_route_context
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
async def test_router_uses_only_the_eight_most_recent_conversation_messages() -> None:
    messages = [
        ChatMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=f"message-{index}",
        )
        for index in range(10)
    ]
    model = FakeRouterModel(TripRouteDecision(route="xhs_trip_planner"))
    registry = FakeRegistry(model)

    result = await TripRequestRouter(registry).route(messages)  # type: ignore[arg-type]

    assert result.route == "xhs_trip_planner"
    assert result.source == "llm_router"
    assert registry.create_calls == 1
    assert model.schema is TripRouteDecision
    assert len(model.captured_messages) == 2
    context_content = model.captured_messages[1].content
    assert isinstance(context_content, str)
    context = json.loads(context_content)
    assert context == {
        "recent_messages": [
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"message-{index}",
            }
            for index in range(2, 10)
        ],
        "latest_user_message": "message-8",
    }


def test_route_context_excludes_system_messages_and_limits_message_size() -> None:
    context = build_route_context(
        [
            ChatMessage(role="system", content="internal prompt"),
            ChatMessage(role="user", content=" x " * 5_000),
        ]
    )

    assert len(context.recent_messages) == 1
    assert context.recent_messages[0].role == "user"
    assert len(context.recent_messages[0].content) == 4_000
    assert context.latest_user_message == context.recent_messages[0].content


@pytest.mark.parametrize("route", ["general_agent", "xhs_trip_planner"])
def test_route_decision_accepts_only_binary_routes(route: str) -> None:
    assert TripRouteDecision.model_validate({"route": route}).route == route


@pytest.mark.parametrize("route", ["clarify", "trip_planner", "other"])
def test_route_decision_rejects_removed_routes(route: str) -> None:
    with pytest.raises(ValidationError):
        TripRouteDecision.model_validate({"route": route})


@pytest.mark.asyncio
async def test_router_prompt_routes_mixed_planning_requests_to_xhs() -> None:
    model = FakeRouterModel(TripRouteDecision(route="xhs_trip_planner"))
    registry = FakeRegistry(model)

    result = await TripRequestRouter(registry).route(  # type: ignore[arg-type]
        [ChatMessage(role="user", content="帮我做成都三日游攻略，顺便查一下酒店")]
    )

    prompt = model.captured_messages[0].content
    assert isinstance(prompt, str)
    assert result.route == "xhs_trip_planner"
    assert "general_agent" in prompt
    assert "xhs_trip_planner" in prompt
    assert "混合" in prompt
    assert "clarify" not in prompt


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
                TripRouteDecision(route="xhs_trip_planner"),
                delay=0.02,
            ),
            timeout_seconds=0.001,
        ),
    ],
)
async def test_router_failures_always_fall_back_to_general_agent(
    registry: FakeRegistry,
) -> None:
    result = await TripRequestRouter(registry).route(  # type: ignore[arg-type]
        [ChatMessage(role="user", content="帮我规划成都四日游并查酒店")]
    )

    assert result.route == "general_agent"
    assert result.source == "fallback"


@pytest.mark.asyncio
async def test_recent_plan_adjustment_is_available_to_router_context() -> None:
    model = FakeRouterModel(TripRouteDecision(route="xhs_trip_planner"))
    registry = FakeRegistry(model)

    result = await TripRequestRouter(registry).route(  # type: ignore[arg-type]
        [
            ChatMessage(role="assistant", content="## 第 1 天\n宽窄巷子"),
            ChatMessage(role="user", content="第二天轻松一点"),
        ]
    )

    assert result.route == "xhs_trip_planner"
    context = json.loads(model.captured_messages[1].content)
    assert context["latest_user_message"] == "第二天轻松一点"
    assert [item["role"] for item in context["recent_messages"]] == [
        "assistant",
        "user",
    ]
