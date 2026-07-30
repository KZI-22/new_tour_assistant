from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.agents.specialist_runtime import SpecialistRuntime
from app.schemas.agent_runtime import (
    AgentStatusEvent,
    AgentToolArtifact,
    MissingField,
    SpecialistResult,
    SupervisorDecision,
    SupervisorTask,
)
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import MessageDeltaEvent, ToolCallEvent
from app.services.multi_agent_orchestrator import MultiAgentOrchestrator
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict


class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str


class _StructuredRunnable:
    def __init__(self, decision: SupervisorDecision) -> None:
        self._decision = decision

    async def ainvoke(self, _: Any) -> SupervisorDecision:
        return self._decision


class _BoundModel:
    def __init__(self, tool_names: set[str], shared: _ModelFactory) -> None:
        self._tool_names = tool_names
        self._shared = shared
        self._round = 0

    async def ainvoke(self, _: Any) -> AIMessage:
        self._round += 1
        if self._round == 1:
            tool_name = next(iter(self._tool_names))
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool_name,
                        "args": {"query": tool_name},
                        "id": f"call-{tool_name}",
                    }
                ],
            )
        return AIMessage(
            content=json.dumps(
                {
                    "status": "success",
                    "summary": "查询完成",
                    "data": {"source": sorted(self._tool_names)},
                    "missing_fields": [],
                    "error_code": None,
                },
                ensure_ascii=False,
            )
        )


class _UniversalModel:
    def __init__(self, shared: _ModelFactory) -> None:
        self._shared = shared

    def with_structured_output(self, _: type[Any]) -> _StructuredRunnable:
        self._shared.supervisor_calls += 1
        return _StructuredRunnable(self._shared.decision)

    def bind_tools(self, tools: list[StructuredTool]) -> _BoundModel:
        names = {tool.name for tool in tools}
        self._shared.bound_tool_sets.append(names)
        return _BoundModel(names, self._shared)

    async def astream(self, _: Any) -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="最终汇总")


class _ModelFactory:
    def __init__(self, decision: SupervisorDecision) -> None:
        self.decision = decision
        self.bound_tool_sets: list[set[str]] = []
        self.supervisor_calls = 0

    def __call__(self, _: str) -> _UniversalModel:
        return _UniversalModel(self)


def _gated_tool(
    name: str,
    started: set[str],
    both_started: asyncio.Event,
) -> StructuredTool:
    async def run(query: str) -> dict[str, object]:
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return {
            "success": True,
            "provider": "fake",
            "data": {"query": query},
            "duration_ms": 1,
        }

    return StructuredTool.from_function(
        coroutine=run,
        name=name,
        description=f"Run {name}",
        args_schema=QueryInput,
    )


@pytest.mark.asyncio
async def test_combined_request_runs_isolated_specialists_concurrently() -> None:
    decision = SupervisorDecision(
        mode="delegate",
        tasks=[
            SupervisorTask(agent="itinerary", instruction="生成西安三日攻略"),
            SupervisorTask(agent="transport", instruction="查询上海到西安高铁"),
        ],
    )
    models = _ModelFactory(decision)
    started: set[str] = set()
    both_started = asyncio.Event()
    orchestrator = MultiAgentOrchestrator(
        models,  # type: ignore[arg-type]
        [
            _gated_tool("ai_search", started, both_started),
            _gated_tool("search_train", started, both_started),
        ],
        max_tool_rounds=2,
        supervisor_timeout_seconds=1,
        agent_timeout_seconds=2,
        tool_timeout_seconds=2,
    )

    events = [
        event
        async for event in orchestrator.stream(
            "test",
            [ChatMessage(role="user", content="规划西安三日游并查高铁")],
            execution_context=None,
        )
    ]

    assert started == {"ai_search", "search_train"}
    assert {"ai_search"} in models.bound_tool_sets
    assert {"search_train"} in models.bound_tool_sets
    assert all(
        names <= {"ai_search"} or names <= {"search_train"}
        for names in models.bound_tool_sets
    )
    assert any(
        isinstance(event, AgentStatusEvent)
        and event.agent == "itinerary"
        and event.status == "success"
        for event in events
    )
    assert {
        event.agent for event in events if isinstance(event, ToolCallEvent)
    } == {"itinerary", "transport"}
    assert (
        "".join(event.delta for event in events if isinstance(event, MessageDeltaEvent))
        == "最终汇总"
    )


class _ResumeBoundModel:
    def __init__(self, tool_names: set[str]) -> None:
        self.tool_names = tool_names
        self.round = 0

    async def ainvoke(self, _: Any) -> AIMessage:
        self.round += 1
        if self.round == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "amap_get_weather",
                        "args": {"query": "2026-08-10 西安"},
                        "id": "weather-call",
                    }
                ],
            )
        return AIMessage(
            content=(
                '{"status":"success","summary":"天气已补齐","data":{"weather":"sunny"},'
                '"missing_fields":[],"error_code":null}'
            )
        )


class _ResumeModel:
    def __init__(self, bound_sets: list[set[str]]) -> None:
        self.bound_sets = bound_sets

    def bind_tools(self, tools: list[StructuredTool]) -> _ResumeBoundModel:
        names = {tool.name for tool in tools}
        self.bound_sets.append(names)
        return _ResumeBoundModel(names)


@pytest.mark.asyncio
async def test_resume_filters_previously_successful_tools() -> None:
    ai_calls: list[str] = []
    weather_calls: list[str] = []

    async def ai_search(query: str) -> dict[str, object]:
        ai_calls.append(query)
        return {"success": True, "provider": "fake", "data": {}, "duration_ms": 1}

    async def weather(query: str) -> dict[str, object]:
        weather_calls.append(query)
        return {
            "success": True,
            "provider": "fake",
            "data": {"forecast": "sunny"},
            "duration_ms": 1,
        }

    ai_tool = StructuredTool.from_function(
        coroutine=ai_search,
        name="ai_search",
        description="AI search",
        args_schema=QueryInput,
    )
    weather_tool = StructuredTool.from_function(
        coroutine=weather,
        name="amap_get_weather",
        description="Weather",
        args_schema=QueryInput,
    )
    bound_sets: list[set[str]] = []
    runtime = SpecialistRuntime(
        lambda _: _ResumeModel(bound_sets),  # type: ignore[arg-type]
        [ai_tool, weather_tool],
        max_tool_rounds=2,
        model_timeout_seconds=1,
        tool_timeout_seconds=1,
        log_writer=None,
    )
    previous = SpecialistResult(
        status="partial",
        summary="攻略已生成，缺少日期",
        data={"draft": "西安三日攻略"},
        missing_fields=[
            MissingField(field="travel_date", prompt="请补充出行日期。")
        ],
        artifacts=[
            AgentToolArtifact(
                tool_call_id="ai-call",
                tool_name="ai_search",
                arguments={"query": "西安三日攻略"},
                success=True,
                data={"draft": "西安三日攻略"},
                duration_ms=1,
            )
        ],
    )
    emitted: list[object] = []

    async def emit(event: object) -> None:
        emitted.append(event)

    result = await runtime.run(
        "test",
        agent="itinerary",
        instruction="用户补充 2026-08-10 出发",
        run_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        execution_context=None,
        previous=previous,
        emit=emit,  # type: ignore[arg-type]
    )

    assert bound_sets == [{"amap_get_weather"}]
    assert ai_calls == []
    assert weather_calls == ["2026-08-10 西安"]
    assert result.status == "success"
    assert {artifact.tool_name for artifact in result.artifacts} == {
        "ai_search",
        "amap_get_weather",
    }


def test_supervisor_decision_rejects_cross_domain_duplicates() -> None:
    with pytest.raises(ValueError, match="at most one task"):
        SupervisorDecision(
            mode="delegate",
            tasks=[
                SupervisorTask(agent="hotel", instruction="查西安酒店"),
                SupervisorTask(agent="hotel", instruction="查钟楼附近酒店"),
            ],
        )
