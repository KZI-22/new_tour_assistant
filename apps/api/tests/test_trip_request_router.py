from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from app.schemas.chat import ChatMessage
from app.schemas.routing import TripRouteDecision
from app.services.trip_request_router import TripRequestRouter, build_route_context
from langchain_core.messages import AIMessage
from pydantic import ValidationError


class FakeRouterModel:
    def __init__(
        self,
        value: Any | list[Any],
        *,
        delay: float = 0,
    ) -> None:
        self.values = value if isinstance(value, list) else [value]
        self.delay = delay
        self.captured_messages: list[Any] = []
        self.calls = 0

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.captured_messages.extend(messages)
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, AIMessage):
            return value
        if isinstance(value, TripRouteDecision):
            return AIMessage(content=value.model_dump_json())
        if isinstance(value, str):
            return AIMessage(content=value)
        return AIMessage(content=json.dumps(value))


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
    model = FakeRouterModel(TripRouteDecision(route="trip_planner"))
    registry = FakeRegistry(model)

    result = await TripRequestRouter(registry).route(messages)  # type: ignore[arg-type]

    assert result.route == "trip_planner"
    assert result.source == "llm_router"
    assert registry.create_calls == 1
    assert model.calls == 1
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


@pytest.mark.parametrize("route", ["general_agent", "trip_planner"])
def test_route_decision_accepts_only_binary_routes(route: str) -> None:
    assert TripRouteDecision.model_validate({"route": route}).route == route


@pytest.mark.parametrize("route", ["clarify", "xhs_trip_planner", "other"])
def test_route_decision_rejects_removed_routes(route: str) -> None:
    with pytest.raises(ValidationError):
        TripRouteDecision.model_validate({"route": route})


@pytest.mark.asyncio
async def test_router_prompt_routes_mixed_planning_requests_to_trip_planner() -> None:
    model = FakeRouterModel(TripRouteDecision(route="trip_planner"))
    registry = FakeRegistry(model)

    result = await TripRequestRouter(registry).route(  # type: ignore[arg-type]
        [ChatMessage(role="user", content="帮我做成都三日游攻略，顺便查一下酒店")]
    )

    prompt = model.captured_messages[0].content
    assert isinstance(prompt, str)
    assert result.route == "trip_planner"
    assert "general_agent" in prompt
    assert "trip_planner" in prompt
    assert "不决定" in prompt
    assert "混合" in prompt
    assert "clarify" not in prompt
    assert "JSON" in prompt
    assert '{"route":"general_agent"}' in prompt
    assert '{"route":"trip_planner"}' in prompt
    assert "不要输出 Markdown" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "registry",
    [
        FakeRegistry(None, factory_error=RuntimeError("not configured")),
        FakeRegistry(FakeRouterModel(NotImplementedError("model unavailable"))),
        FakeRegistry(FakeRouterModel({"route": "invalid"})),
        FakeRegistry(FakeRouterModel(RuntimeError("provider failed"))),
        FakeRegistry(
            FakeRouterModel(
                TripRouteDecision(route="trip_planner"),
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
async def test_router_retries_once_after_schema_validation_failure() -> None:
    model = FakeRouterModel(
        [
            {"decision": "trip_planner", "reasoning": "planning request"},
            TripRouteDecision(route="trip_planner"),
        ]
    )
    registry = FakeRegistry(model)

    result = await TripRequestRouter(registry).route(  # type: ignore[arg-type]
        [ChatMessage(role="user", content="帮我规划成都四日游")]
    )

    assert result.route == "trip_planner"
    assert result.source == "llm_router"
    assert model.calls == 2
    retry_prompt = model.captured_messages[3].content
    assert isinstance(retry_prompt, str)
    assert "上一次输出未通过 JSON 格式或字段校验" in retry_prompt


@pytest.mark.asyncio
async def test_router_never_attempts_more_than_one_retry() -> None:
    model = FakeRouterModel(
        [
            "not json",
            "still not json",
            TripRouteDecision(route="trip_planner"),
        ]
    )
    registry = FakeRegistry(model)

    result = await TripRequestRouter(registry).route(  # type: ignore[arg-type]
        [ChatMessage(role="user", content="帮我规划成都四日游")]
    )

    assert result.route == "general_agent"
    assert result.source == "fallback"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_recent_plan_adjustment_is_available_to_router_context() -> None:
    model = FakeRouterModel(TripRouteDecision(route="trip_planner"))
    registry = FakeRegistry(model)

    result = await TripRequestRouter(registry).route(  # type: ignore[arg-type]
        [
            ChatMessage(role="assistant", content="## 第 1 天\n宽窄巷子"),
            ChatMessage(role="user", content="第二天轻松一点"),
        ]
    )

    assert result.route == "trip_planner"
    context = json.loads(model.captured_messages[1].content)
    assert context["latest_user_message"] == "第二天轻松一点"
    assert [item["role"] for item in context["recent_messages"]] == [
        "assistant",
        "user",
    ]
