from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from app.schemas.agent_runtime import (
    AgentToolArtifact,
    SpecialistAgent,
    SpecialistDecision,
    SpecialistResult,
)
from app.schemas.tool_execution import ChatStreamEvent, ToolCallEvent, ToolResultEvent
from app.services.tool_call_log_service import ToolCallLogWriter
from app.services.tool_execution import ToolExecutionContext, ToolExecutor

logger = logging.getLogger(__name__)

_SPECIALIST_PROMPTS: dict[SpecialistAgent, str] = {
    "itinerary": """你是隔离运行的行程规划 Agent。你只能处理以下能力：
- ai_search：生成多日旅游攻略草案。
- search_poi：查询具体景点。
- keyword_search：在景点已经确认后查询门票、讲解、演出和旅游商品。
- amap_get_weather：查询城市天气。

按用户任务选择必要能力，不要为了展示而调用无关工具。日期已知时，可在同一轮并行调用 ai_search 和
天气；日期缺失时仍应先完成 ai_search，并以 partial 返回行程草案，同时只把天气所需日期列为
missing_fields。恢复执行时不得重复调用先前 artifacts 中成功的工具。
不得编造景点、价格、门票或天气。""",
    "transport": """你是隔离运行的交通查询 Agent。你只能调用 search_flight 和 search_train。
根据用户意图执行单项查询或交通方式比较。由你判断出发地、目的地、日期等工具条件是否齐全；
不齐全时不要猜测，返回 needs_input。不得编造班次、价格或余票。""",
    "hotel": """你是隔离运行的酒店查询 Agent。你只能调用 search_hotel。由你理解目的地、入住、
退房、预算、星级、床型和位置偏好，并判断查询条件是否完整；必填条件不足时返回 needs_input。
不得编造酒店、房价或库存。""",
}

_OUTPUT_INSTRUCTION = """

完成工具调用后，只输出一个符合下列约束的 JSON 对象，不要输出 Markdown：
- status: success、partial、needs_input 或 failed。
- summary: 对本 Agent 结果的简洁说明。
- data: 仅包含工具已返回或用户已提供的业务结果；无结果时为 null。
- missing_fields: 每项含 field、prompt、reason；success 时必须为空。
- error_code: failed 时必须提供稳定错误码，其他状态为 null。
partial 仅用于已经取得可保留结果、但仍需用户补字段的情况。"""


class EventEmitter(Protocol):
    async def __call__(self, event: ChatStreamEvent) -> None: ...


class SpecialistRuntime:
    """One isolated tool loop with an immutable, domain-specific allowlist."""

    def __init__(
        self,
        model_factory: Callable[[str], BaseChatModel],
        tools: Sequence[BaseTool],
        *,
        max_tool_rounds: int,
        model_timeout_seconds: float,
        tool_timeout_seconds: float,
        log_writer: ToolCallLogWriter | None,
    ) -> None:
        self._model_factory = model_factory
        self._tools = tuple(tools)
        self._max_tool_rounds = max_tool_rounds
        self._model_timeout_seconds = model_timeout_seconds
        self._tool_timeout_seconds = tool_timeout_seconds
        self._log_writer = log_writer

    async def run(
        self,
        model_id: str,
        *,
        agent: SpecialistAgent,
        instruction: str,
        run_id: UUID,
        task_id: UUID,
        execution_context: ToolExecutionContext | None,
        previous: SpecialistResult | None,
        emit: EventEmitter,
    ) -> SpecialistResult:
        completed_tool_names = {
            artifact.tool_name
            for artifact in (previous.artifacts if previous is not None else [])
            if artifact.success
        }
        active_tools = [tool for tool in self._tools if tool.name not in completed_tool_names]
        executor = ToolExecutor(
            active_tools,
            timeout_seconds=self._tool_timeout_seconds,
            log_writer=self._log_writer,
        )
        model = self._model_factory(model_id)
        runtime_model = model.bind_tools(active_tools) if active_tools else model
        messages: list[BaseMessage] = [
            SystemMessage(content=_SPECIALIST_PROMPTS[agent] + _OUTPUT_INSTRUCTION),
            HumanMessage(content=_task_prompt(instruction, previous)),
        ]
        artifacts = list(previous.artifacts if previous is not None else [])
        scoped_context = _scoped_context(execution_context, run_id, task_id, agent)

        for round_index in range(self._max_tool_rounds + 1):
            response = await asyncio.wait_for(
                runtime_model.ainvoke(messages),
                timeout=self._model_timeout_seconds,
            )
            if not isinstance(response, AIMessage):
                return _failed_result(
                    artifacts,
                    "MODEL_RESPONSE_INVALID",
                    "专业 Agent 返回了无法处理的模型响应。",
                )

            if not response.tool_calls:
                decision = _parse_decision(response)
                if decision is None:
                    text = _message_text(response)
                    if not text:
                        return _failed_result(
                            artifacts,
                            "MODEL_EMPTY_RESPONSE",
                            "专业 Agent 没有返回有效结果。",
                        )
                    decision = SpecialistDecision(
                        status="success",
                        summary="专业 Agent 已返回结果。",
                        data={"agent_response": text},
                    )
                return _merge_result(previous, decision, artifacts)

            if round_index >= self._max_tool_rounds:
                return _failed_result(
                    artifacts,
                    "TOOL_LOOP_LIMIT",
                    "专业 Agent 的工具调用次数已达到上限。",
                )

            messages.append(response)
            prepared = executor.prepare_calls(
                [dict(tool_call) for tool_call in response.tool_calls],
                round_index=round_index,
            )
            for call in prepared:
                await emit(
                    cast(
                        ToolCallEvent,
                        call.event.model_copy(
                            update={
                                "agent_run_id": str(run_id),
                                "agent_task_id": str(task_id),
                                "agent": agent,
                            }
                        ),
                    )
                )

            outcomes = await executor.execute_many(prepared, context=scoped_context)
            for call, outcome in zip(prepared, outcomes, strict=True):
                messages.append(outcome.message)
                event = cast(
                    ToolResultEvent,
                    outcome.event.model_copy(
                        update={
                            "agent_run_id": str(run_id),
                            "agent_task_id": str(task_id),
                            "agent": agent,
                        }
                    ),
                )
                await emit(event)
                artifacts.append(
                    AgentToolArtifact(
                        tool_call_id=call.tool_call_id,
                        tool_name=call.tool_name,
                        arguments=call.public_arguments,
                        success=outcome.result.success,
                        data=outcome.result.data,
                        error_code=(
                            outcome.result.error.code if outcome.result.error is not None else None
                        ),
                        duration_ms=outcome.result.metadata.duration_ms,
                    )
                )

        return _failed_result(
            artifacts,
            "TOOL_LOOP_LIMIT",
            "专业 Agent 的工具调用次数已达到上限。",
        )


def _scoped_context(
    context: ToolExecutionContext | None,
    run_id: UUID,
    task_id: UUID,
    agent: SpecialistAgent,
) -> ToolExecutionContext | None:
    if context is None:
        return None
    return ToolExecutionContext(
        conversation_id=context.conversation_id,
        assistant_message_id=context.assistant_message_id,
        agent_run_id=run_id,
        agent_task_id=task_id,
        agent_name=agent,
    )


def _task_prompt(instruction: str, previous: SpecialistResult | None) -> str:
    payload: dict[str, Any] = {"task": instruction}
    if previous is not None:
        payload["previous_result"] = previous.model_dump(mode="json")
        payload["resume_rule"] = (
            "保留 previous_result，只补齐缺失部分；不得重复调用其中 success=true 的工具。"
        )
    payload["output_schema"] = SpecialistDecision.model_json_schema()
    return json.dumps(payload, ensure_ascii=False)


def _parse_decision(response: AIMessage) -> SpecialistDecision | None:
    text = _message_text(response)
    if not text:
        return None
    try:
        return SpecialistDecision.model_validate_json(_extract_json(text))
    except (ValueError, ValidationError):
        logger.info("Specialist final response was not structured JSON")
        return None


def _merge_result(
    previous: SpecialistResult | None,
    decision: SpecialistDecision,
    artifacts: list[AgentToolArtifact],
) -> SpecialistResult:
    data = decision.data
    if previous is not None and previous.data is not None and decision.data is not None:
        data = {"previous": previous.data, "current": decision.data}
    elif previous is not None and decision.data is None:
        data = previous.data
    return SpecialistResult(
        **decision.model_dump(exclude={"data"}),
        data=data,
        artifacts=_deduplicate_artifacts(artifacts),
    )


def _deduplicate_artifacts(
    artifacts: list[AgentToolArtifact],
) -> list[AgentToolArtifact]:
    seen: set[tuple[str, str]] = set()
    unique: list[AgentToolArtifact] = []
    for artifact in artifacts:
        key = (
            artifact.tool_name,
            json.dumps(artifact.arguments, ensure_ascii=False, sort_keys=True, default=str),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(artifact)
    return unique


def _failed_result(
    artifacts: list[AgentToolArtifact],
    error_code: str,
    summary: str,
) -> SpecialistResult:
    return SpecialistResult(
        status="failed",
        summary=summary,
        error_code=error_code,
        artifacts=_deduplicate_artifacts(artifacts),
    )


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
            chunks.append(cast(str, item["text"]))
    return "".join(chunks)


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, count=1, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped, count=1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model response does not contain a JSON object")
    return stripped[start : end + 1]


__all__ = ["SpecialistRuntime"]
