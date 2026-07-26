from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import pytest
from app.core.model_registry import UnavailableModelError
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import MessageDeltaEvent, ToolCallEvent, ToolResultEvent
from app.services.agent_executor import AgentExecutor, ToolLoopLimitError
from app.services.chat_service import ChatService
from app.services.tool_call_log_service import ToolCallLogEntry
from app.services.tool_execution import ToolExecutionContext, ToolExecutor
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict


class ValueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def tool_call_chunk(
    name: str,
    args: dict[str, object],
    call_id: str,
    index: int,
) -> dict[str, object]:
    return {
        "name": name,
        "args": json.dumps(args, ensure_ascii=False),
        "id": call_id,
        "index": index,
    }


class FakeModel:
    """Streams each scripted response so the executor sees provider-style chunks."""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self.inputs: list[list[BaseMessage]] = []

    async def astream(self, input: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.inputs.append(list(input))
        response = self._responses.pop(0)
        text = str(response.content)
        for start in range(0, len(text), 4):
            yield AIMessageChunk(content=text[start : start + 4])
        if response.tool_calls:
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    tool_call_chunk(call["name"], call["args"], str(call["id"]), index)
                    for index, call in enumerate(response.tool_calls)
                ],
            )


class GatedStreamingModel:
    """Emits one chunk, then blocks until the test releases the rest."""

    def __init__(self, first: str, rest: str) -> None:
        self._first = first
        self._rest = rest
        self.released = asyncio.Event()

    async def astream(self, input: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        del input
        yield AIMessageChunk(content=self._first)
        await self.released.wait()
        yield AIMessageChunk(content=self._rest)


def value_tool(
    name: str,
    calls: list[str],
    *,
    fail: bool = False,
    delay: float = 0,
) -> StructuredTool:
    async def run(value: str) -> dict[str, object]:
        calls.append(value)
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise RuntimeError("private provider detail")
        return {
            "success": True,
            "provider": "fake",
            "command": ["private-provider-command"],
            "data": {"value": value},
            "duration_ms": 1,
        }

    return StructuredTool.from_function(
        coroutine=run,
        name=name,
        description=f"Run {name}",
        args_schema=ValueInput,
    )


def empty_tool(name: str, calls: list[str], value: str) -> StructuredTool:
    async def run() -> dict[str, object]:
        calls.append(value)
        return {
            "success": True,
            "provider": "fake",
            "data": {"value": value},
            "duration_ms": 1,
        }

    return StructuredTool.from_function(
        coroutine=run,
        name=name,
        description=f"Run {name}",
        args_schema=EmptyInput,
    )


async def collect(executor: AgentExecutor) -> list[object]:
    return [event async for event in executor.stream([])]


def event_kinds(events: list[object]) -> list[str]:
    """Event type order with consecutive text deltas collapsed into one entry."""
    kinds: list[str] = []
    for event in events:
        name = type(event).__name__
        if not kinds or kinds[-1] != name:
            kinds.append(name)
    return kinds


def streamed_text(events: list[object]) -> str:
    return "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))


@pytest.mark.asyncio
async def test_plain_conversation_does_not_execute_tools() -> None:
    calls: list[str] = []
    model = FakeModel([AIMessage(content="你好，我可以帮你规划旅行。")])
    executor = AgentExecutor(model, ToolExecutor([value_tool("echo", calls)]))

    events = await collect(executor)

    assert calls == []
    assert (
        "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
        == "你好，我可以帮你规划旅行。"
    )


@pytest.mark.asyncio
async def test_text_reaches_the_user_before_generation_finishes() -> None:
    model = GatedStreamingModel("南京今天", "天气晴。")
    executor = AgentExecutor(model, ToolExecutor([]))
    stream = executor.stream([])

    first = await asyncio.wait_for(anext(stream), timeout=1)

    assert isinstance(first, MessageDeltaEvent)
    assert first.delta == "南京今天"
    assert not model.released.is_set()

    model.released.set()
    remaining = [event async for event in stream]

    assert (
        "".join(event.delta for event in remaining if isinstance(event, MessageDeltaEvent))
        == "天气晴。"
    )


@pytest.mark.asyncio
async def test_text_from_a_tool_calling_round_reaches_the_user() -> None:
    calls: list[str] = []
    model = FakeModel(
        [
            AIMessage(
                content="让我先查一下天气。",
                tool_calls=[{"name": "echo", "args": {"value": "南京"}, "id": "call-one"}],
            ),
            AIMessage(content="南京今天晴。"),
        ]
    )
    executor = AgentExecutor(model, ToolExecutor([value_tool("echo", calls)]))

    events = await collect(executor)

    assert calls == ["南京"]
    assert streamed_text(events) == "让我先查一下天气。南京今天晴。"
    assert event_kinds(events) == [
        "MessageDeltaEvent",
        "ToolCallEvent",
        "ToolResultEvent",
        "MessageDeltaEvent",
    ]


@pytest.mark.asyncio
async def test_one_empty_model_response_is_retried_once() -> None:
    model = FakeModel([AIMessage(content=""), AIMessage(content="重试后正常回答。")])
    executor = AgentExecutor(model, ToolExecutor([]))

    events = await collect(executor)

    assert len(model.inputs) == 2
    assert (
        "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
        == "重试后正常回答。"
    )


@pytest.mark.asyncio
async def test_model_without_tool_calling_returns_safe_business_error() -> None:
    class UnsupportedModel:
        def bind_tools(self, tools: object) -> object:
            del tools
            raise NotImplementedError("private provider stack")

    class Registry:
        def create_model(self, model_id: str) -> UnsupportedModel:
            assert model_id == "unsupported"
            return UnsupportedModel()

    service = ChatService(Registry(), [])  # type: ignore[arg-type]

    with pytest.raises(UnavailableModelError, match="does not support tool calling"):
        await anext(
            service.stream(
                "unsupported",
                [ChatMessage(role="user", content="你好")],
            )
        )


@pytest.mark.asyncio
async def test_single_tool_call_writes_correlated_tool_message() -> None:
    calls: list[str] = []
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "echo", "args": {"value": "航班"}, "id": "call-one"}],
            ),
            AIMessage(content="已根据实时结果完成回答。"),
        ]
    )
    executor = AgentExecutor(model, ToolExecutor([value_tool("echo", calls)]))

    events = await collect(executor)

    assert calls == ["航班"]
    assert event_kinds(events) == [
        "ToolCallEvent",
        "ToolResultEvent",
        "MessageDeltaEvent",
    ]
    tool_messages = [message for message in model.inputs[1] if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call-one"
    payload = json.loads(str(tool_messages[0].content))
    assert payload["success"] is True
    assert payload["tool_name"] == "echo"
    assert "command" not in payload


@pytest.mark.asyncio
async def test_serial_tool_rounds_preserve_message_order() -> None:
    calls: list[str] = []
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "locate", "args": {}, "id": "city-call"},
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "weather",
                        "args": {"value": "南京"},
                        "id": "weather-call",
                    }
                ],
            ),
            AIMessage(content="根据网络位置推测，南京今天天气晴。"),
        ]
    )
    tools = [empty_tool("locate", calls, "南京"), value_tool("weather", calls)]
    executor = AgentExecutor(model, ToolExecutor(tools))

    events = await collect(executor)

    assert calls == ["南京", "南京"]
    assert [
        event.tool_call_id
        for event in events
        if isinstance(event, (ToolCallEvent, ToolResultEvent))
    ] == ["city-call", "city-call", "weather-call", "weather-call"]
    tool_messages = [message for message in model.inputs[2] if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tool_messages] == [
        "city-call",
        "weather-call",
    ]


@pytest.mark.asyncio
async def test_parallel_calls_keep_success_when_peer_fails() -> None:
    calls: list[str] = []
    active = 0
    max_active = 0

    async def flight(value: str) -> dict[str, object]:
        nonlocal active, max_active
        calls.append(value)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"success": True, "provider": "fake", "data": [value], "duration_ms": 2}

    async def train(value: str) -> dict[str, object]:
        nonlocal active, max_active
        calls.append(value)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        raise RuntimeError("provider stack must not escape")

    tools = [
        StructuredTool.from_function(
            coroutine=flight,
            name="search_flight",
            description="Search flights",
            args_schema=ValueInput,
        ),
        StructuredTool.from_function(
            coroutine=train,
            name="search_train",
            description="Search trains",
            args_schema=ValueInput,
        ),
    ]
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_flight",
                        "args": {"value": "flight"},
                        "id": "flight-call",
                    },
                    {
                        "name": "search_train",
                        "args": {"value": "train"},
                        "id": "train-call",
                    },
                ],
            ),
            AIMessage(content="航班成功，火车查询失败。"),
        ]
    )
    executor = AgentExecutor(model, ToolExecutor(tools))

    events = await collect(executor)

    assert max_active == 2
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert [(event.tool_call_id, event.success) for event in results] == [
        ("flight-call", True),
        ("train-call", False),
    ]
    assert results[1].error_code == "TOOL_EXECUTION_FAILED"
    assert "stack" not in results[1].summary


@pytest.mark.asyncio
async def test_unknown_tool_and_invalid_arguments_become_tool_results() -> None:
    calls: list[str] = []
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "missing", "args": {"value": "x"}, "id": "missing-call"},
                    {"name": "echo", "args": {}, "id": "invalid-call"},
                ],
            ),
            AIMessage(content="工具不可用，请补充信息后重试。"),
        ]
    )
    executor = AgentExecutor(model, ToolExecutor([value_tool("echo", calls)]))

    events = await collect(executor)

    assert calls == []
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert [event.error_code for event in results] == [
        "TOOL_NOT_FOUND",
        "TOOL_ARGUMENT_INVALID",
    ]


@pytest.mark.asyncio
async def test_tool_timeout_is_safe_and_model_can_continue() -> None:
    async def blocked(value: str) -> dict[str, object]:
        del value
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    tool = StructuredTool.from_function(
        coroutine=blocked,
        name="slow_tool",
        description="Never finishes",
        args_schema=ValueInput,
    )
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "slow_tool", "args": {"value": "x"}, "id": "slow-call"}],
            ),
            AIMessage(content="查询超时，请稍后重试。"),
        ]
    )
    executor = AgentExecutor(model, ToolExecutor([tool], timeout_seconds=0.01))

    events = await collect(executor)

    result = next(event for event in events if isinstance(event, ToolResultEvent))
    assert result.success is False
    assert result.error_code == "PROVIDER_TIMEOUT"


@pytest.mark.asyncio
async def test_cancellation_stops_in_flight_tool_and_skips_next_model_round() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(value: str) -> dict[str, object]:
        del value
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    tool = StructuredTool.from_function(
        coroutine=blocked,
        name="blocked_tool",
        description="Wait until cancelled",
        args_schema=ValueInput,
    )
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "blocked_tool",
                        "args": {"value": "x"},
                        "id": "blocked-call",
                    }
                ],
            ),
            AIMessage(content="must not be reached"),
        ]
    )
    executor = AgentExecutor(model, ToolExecutor([tool], timeout_seconds=60))
    stream = executor.stream([])
    assert isinstance(await anext(stream), ToolCallEvent)
    next_event = asyncio.create_task(anext(stream))
    await asyncio.wait_for(started.wait(), timeout=1)

    next_event.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_event
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await stream.aclose()

    assert len(model.inputs) == 1


@pytest.mark.asyncio
async def test_tool_round_limit_stops_repeated_calls() -> None:
    calls: list[str] = []

    class RepeatingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def astream(self, input: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
            del input
            self.calls += 1
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    tool_call_chunk("echo", {"value": str(self.calls)}, f"call-{self.calls}", 0)
                ],
            )

    model = RepeatingModel()
    executor = AgentExecutor(
        model,
        ToolExecutor([value_tool("echo", calls)]),
        max_tool_rounds=2,
    )

    with pytest.raises(ToolLoopLimitError) as raised:
        _ = [event async for event in executor.stream([])]

    assert raised.value.code == "TOOL_LOOP_LIMIT"
    assert calls == ["1", "2"]
    assert model.calls == 3


@pytest.mark.asyncio
async def test_log_arguments_are_redacted_and_log_failure_is_non_fatal() -> None:
    entries: list[ToolCallLogEntry] = []

    class LogWriter:
        async def record(self, entry: ToolCallLogEntry) -> None:
            entries.append(entry)
            raise RuntimeError("database unavailable")

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "missing",
                        "args": {"api_key": "private-key", "city": "南京"},
                        "id": "missing-call",
                    }
                ],
            ),
            AIMessage(content="查询工具不可用。"),
        ]
    )
    context = ToolExecutionContext(uuid.uuid4(), uuid.uuid4())
    executor = AgentExecutor(
        model,
        ToolExecutor([], log_writer=LogWriter()),
    )

    events = [event async for event in executor.stream([], execution_context=context)]

    assert any(isinstance(event, MessageDeltaEvent) for event in events)
    assert entries[0].arguments == {"api_key": "[REDACTED]", "city": "南京"}


@pytest.mark.asyncio
async def test_provider_error_code_is_sanitized_and_recorded() -> None:
    entries: list[ToolCallLogEntry] = []

    class LogWriter:
        async def record(self, entry: ToolCallLogEntry) -> None:
            entries.append(entry)

    async def rate_limited(value: str) -> dict[str, object]:
        del value
        return {
            "success": False,
            "provider": "amap",
            "error_code": "RATE_LIMITED",
            "provider_error_code": "10003",
        }

    tool = StructuredTool.from_function(
        coroutine=rate_limited,
        name="amap_search_places",
        description="rate limited",
        args_schema=ValueInput,
    )
    executor = ToolExecutor([tool], log_writer=LogWriter())
    prepared = executor.prepare_calls(
        [{"id": "rate-call", "name": "amap_search_places", "args": {"value": "x"}}],
        round_index=0,
    )

    outcomes = await executor.execute_many(
        prepared,
        context=ToolExecutionContext(uuid.uuid4(), uuid.uuid4()),
    )

    assert outcomes[0].event.provider_error_code == "10003"
    assert outcomes[0].result.error is not None
    assert outcomes[0].result.error.provider_error_code == "10003"
    assert entries[0].provider_error_code == "10003"
