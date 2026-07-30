from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from app.agents.answer_agent import AnswerAgent
from app.agents.specialist_runtime import SpecialistRuntime
from app.agents.supervisor_agent import SupervisorAgent, resume_decision
from app.schemas.agent_runtime import (
    AgentDebugEvent,
    AgentName,
    AgentRunSnapshot,
    AgentRunStatus,
    AgentStatusEvent,
    AgentTaskSnapshot,
    AgentTaskStatus,
    AgentTraceEvent,
    MissingField,
    SpecialistAgent,
    SpecialistResult,
    SupervisorDecision,
    SupervisorTask,
)
from app.schemas.chat import ChatMessage
from app.schemas.tool_execution import (
    ChatStreamEvent,
    MessageDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from app.services.agent_state_service import AgentStateStore
from app.services.tool_call_log_service import ToolCallLogWriter
from app.services.tool_execution import ToolExecutionContext

logger = logging.getLogger(__name__)

_AGENT_DISPLAY_NAMES: dict[AgentName, str] = {
    "supervisor": "主 Agent",
    "itinerary": "行程规划 Agent",
    "transport": "交通查询 Agent",
    "hotel": "酒店查询 Agent",
    "answer": "回答 Agent",
}

_TOOL_ALLOWLISTS: dict[SpecialistAgent, frozenset[str]] = {
    "itinerary": frozenset(
        {
            "ai_search",
            "search_poi",
            "keyword_search",
            "amap_get_weather",
        }
    ),
    "transport": frozenset({"search_flight", "search_train"}),
    "hotel": frozenset({"search_hotel"}),
}


class MultiAgentOrchestrator:
    """Supervisor-owned task state coordinating fully isolated specialist runtimes."""

    def __init__(
        self,
        model_factory: Callable[[str], BaseChatModel],
        tools: Sequence[BaseTool],
        *,
        max_tool_rounds: int,
        supervisor_timeout_seconds: float,
        agent_timeout_seconds: float,
        tool_timeout_seconds: float,
        log_writer: ToolCallLogWriter | None = None,
        state_store: AgentStateStore | None = None,
    ) -> None:
        self._model_factory = model_factory
        self._supervisor = SupervisorAgent(
            model_factory,
            timeout_seconds=supervisor_timeout_seconds,
        )
        self._answer = AnswerAgent(
            model_factory,
            timeout_seconds=agent_timeout_seconds,
        )
        tools_by_name = {tool.name: tool for tool in tools}
        self._tools_by_agent = {
            agent: tuple(tools_by_name[name] for name in allowlist if name in tools_by_name)
            for agent, allowlist in _TOOL_ALLOWLISTS.items()
        }
        self._max_tool_rounds = max_tool_rounds
        self._agent_timeout_seconds = agent_timeout_seconds
        self._tool_timeout_seconds = tool_timeout_seconds
        self._log_writer = log_writer
        self._state_store = state_store

    async def stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        *,
        execution_context: ToolExecutionContext | None,
    ) -> AsyncIterator[ChatStreamEvent]:
        current_request = _latest_user_message(messages)
        conversation_id = (
            execution_context.conversation_id if execution_context is not None else uuid.uuid4()
        )
        assistant_message_id = (
            execution_context.assistant_message_id
            if execution_context is not None
            else uuid.uuid4()
        )
        pending = (
            await self._state_store.find_pending_run(conversation_id)
            if self._state_store is not None
            else None
        )

        if pending is not None:
            run = pending
            decision = resume_decision(pending, current_request)
        else:
            run_id = uuid.uuid4()
            run = await self._create_run(
                run_id,
                conversation_id,
                assistant_message_id,
                current_request,
                (),
            )
            decision = None

        events = _RunEvents(
            run.id,
            assistant_message_id,
            start_sequence=run.last_event_sequence,
            state_store=self._state_store,
        )
        yield await events.status(
            "supervisor",
            "running",
            "正在理解并拆分请求",
        )
        yield await events.trace(
            "supervisor",
            "request_received",
            "running",
            "主 Agent 已接收请求",
            data={"resuming": pending is not None},
        )
        yield await events.status(
            "answer",
            "waiting",
            "等待业务结果",
        )

        if decision is None:
            decision = await self._supervisor.decide(model_id, messages)
        if pending is None and decision.mode == "resume":
            decision = SupervisorDecision(
                mode="delegate",
                tasks=[
                    task.model_copy(update={"task_id": None})
                    for task in decision.tasks
                ],
            )

        if decision.mode == "direct":
            yield await events.trace(
                "supervisor",
                "direct_response_selected",
                "success",
                "主 Agent 选择直接回复",
            )
            yield await events.status(
                "answer",
                "cancelled",
                "当前请求无需回答 Agent",
            )
            async for event in self._answer.stream_direct(model_id, messages):
                yield event
            yield await events.status(
                "supervisor",
                "success",
                "已直接完成回复",
            )
            await self._update_run(run.id, "completed")
            return

        if decision.mode == "delegate":
            task_snapshots = await self._add_tasks(run.id, decision.tasks)
            run = run.model_copy(update={"tasks": task_snapshots})
        else:
            task_snapshots = _resume_tasks(run, decision)
            await self._update_run(run.id, "running")

        yield await events.trace(
            "supervisor",
            "tasks_decomposed",
            "success",
            "主 Agent 已拆分专业任务",
            data={
                "mode": decision.mode,
                "tasks": [
                    {"task_id": str(task.id), "agent": task.agent} for task in task_snapshots
                ],
            },
        )
        yield await events.status(
            "supervisor",
            "success",
            f"已启动 {len(task_snapshots)} 个专业 Agent",
        )
        for task in task_snapshots:
            yield await events.status(
                task.agent,
                "queued",
                "等待运行",
                task_id=task.id,
            )

        queue: asyncio.Queue[ChatStreamEvent | object] = asyncio.Queue()
        sentinel = object()
        results: list[AgentTaskSnapshot] = []

        async def produce() -> None:
            produced = await asyncio.gather(
                *(
                    self._run_task(
                        model_id,
                        task,
                        execution_context,
                        events,
                        queue,
                    )
                    for task in task_snapshots
                )
            )
            results.extend(produced)
            await queue.put(sentinel)

        producer = asyncio.create_task(produce())
        try:
            while True:
                event = await queue.get()
                if event is sentinel:
                    break
                yield event  # type: ignore[misc]
            await producer
        finally:
            if not producer.done():
                producer.cancel()
                with suppress(asyncio.CancelledError):
                    await producer

        all_tasks = _replace_tasks(run.tasks, results)
        missing = _collect_missing_fields(all_tasks)
        if missing:
            await self._update_run(run.id, "needs_input")
            yield await events.status(
                "supervisor",
                "needs_input",
                "已汇总需要补充的信息",
            )
            yield await events.trace(
                "supervisor",
                "input_required",
                "partial",
                "部分结果已保留，等待用户补充",
                data={"fields": [field.field for field in missing]},
            )
            yield MessageDeltaEvent(delta=_clarification_message(missing, all_tasks))
            return

        yield await events.status(
            "answer",
            "running",
            "正在整理业务结果",
        )
        yield await events.trace(
            "answer",
            "answer_started",
            "running",
            "回答 Agent 开始生成最终回复",
            data={
                "sources": [
                    {"task_id": str(task.id), "agent": task.agent, "status": task.status}
                    for task in all_tasks
                ]
            },
        )
        try:
            async for event in self._answer.stream(model_id, current_request, all_tasks):
                yield event
        except Exception:
            yield await events.status(
                "answer",
                "failed",
                "最终回复生成失败",
            )
            await self._update_run(run.id, "failed")
            raise

        final_status = _final_run_status(all_tasks)
        await self._update_run(run.id, final_status)
        yield await events.status(
            "answer",
            "success",
            "已生成最终回复",
        )
        yield await events.trace(
            "answer",
            "answer_completed",
            "success",
            "回答 Agent 已完成",
            data={"run_status": final_status},
        )

    async def _run_task(
        self,
        model_id: str,
        task: AgentTaskSnapshot,
        execution_context: ToolExecutionContext | None,
        events: _RunEvents,
        queue: asyncio.Queue[ChatStreamEvent | object],
    ) -> AgentTaskSnapshot:
        await self._update_task(
            task.id,
            status="running",
            increment_attempt=True,
            instruction=task.instruction,
        )
        await queue.put(
            await events.status(
                task.agent,
                "running",
                _running_detail(task.agent),
                task_id=task.id,
            )
        )
        await queue.put(
            await events.trace(
                task.agent,
                "agent_started",
                "running",
                f"{_AGENT_DISPLAY_NAMES[task.agent]} 已启动",
                task_id=task.id,
            )
        )

        async def emit(event: ChatStreamEvent) -> None:
            await queue.put(event)
            if isinstance(event, ToolCallEvent):
                await queue.put(
                    await events.trace(
                        task.agent,
                        "tool_started",
                        "running",
                        f"开始调用 {event.tool_name}",
                        task_id=task.id,
                        data={
                            "tool_call_id": event.tool_call_id,
                            "tool_name": event.tool_name,
                            "arguments": event.arguments,
                        },
                    )
                )
            elif isinstance(event, ToolResultEvent):
                await queue.put(
                    await events.trace(
                        task.agent,
                        "tool_completed",
                        "success" if event.success else "failed",
                        event.summary,
                        task_id=task.id,
                        duration_ms=event.duration_ms,
                        data={
                            "tool_call_id": event.tool_call_id,
                            "tool_name": event.tool_name,
                            "error_code": event.error_code,
                            "provider_error_code": event.provider_error_code,
                            "process_status": event.process_status,
                            "process_return_code": event.process_return_code,
                            "provider_status": event.provider_status,
                            "parse_status": event.parse_status,
                            "business_status": event.business_status,
                        },
                    )
                )

        allowed_tools = self._tools_by_agent[task.agent]
        runtime = SpecialistRuntime(
            self._model_factory,
            allowed_tools,
            max_tool_rounds=self._max_tool_rounds,
            model_timeout_seconds=self._agent_timeout_seconds,
            tool_timeout_seconds=self._tool_timeout_seconds,
            log_writer=self._log_writer,
        )
        try:
            result = await runtime.run(
                model_id,
                agent=task.agent,
                instruction=task.instruction,
                run_id=task.run_id,
                task_id=task.id,
                execution_context=execution_context,
                previous=task.result,
                emit=emit,
            )
        except TimeoutError:
            result = SpecialistResult(
                status="failed",
                summary="专业 Agent 运行超时。",
                error_code="AGENT_TIMEOUT",
                artifacts=list(task.result.artifacts if task.result else []),
            )
        except asyncio.CancelledError:
            await self._update_task(task.id, status="cancelled")
            raise
        except Exception as exc:
            logger.exception(
                "Specialist runtime failed agent=%s exception_type=%s",
                task.agent,
                type(exc).__name__,
            )
            result = SpecialistResult(
                status="failed",
                summary="专业 Agent 暂时无法完成任务。",
                error_code="AGENT_EXECUTION_FAILED",
                artifacts=list(task.result.artifacts if task.result else []),
            )

        status: AgentTaskStatus = result.status
        await self._update_task(task.id, status=status, result=result)
        await queue.put(
            await events.status(
                task.agent,
                status,
                result.summary,
                task_id=task.id,
            )
        )
        await queue.put(
            await events.trace(
                task.agent,
                "agent_completed",
                _trace_status(status),
                f"{_AGENT_DISPLAY_NAMES[task.agent]} 已结束",
                detail=result.summary,
                task_id=task.id,
                data={
                    "missing_fields": [field.field for field in result.missing_fields],
                    "artifact_count": len(result.artifacts),
                    "error_code": result.error_code,
                },
            )
        )
        return task.model_copy(
            update={
                "status": status,
                "missing_fields": result.missing_fields,
                "result": result,
                "error_code": result.error_code,
                "attempt_count": task.attempt_count + 1,
            }
        )

    async def _create_run(
        self,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        assistant_message_id: uuid.UUID,
        user_request: str,
        tasks: Sequence[SupervisorTask],
    ) -> AgentRunSnapshot:
        if self._state_store is not None:
            return await self._state_store.create_run(
                run_id,
                conversation_id,
                assistant_message_id,
                user_request,
                tasks,
            )
        return AgentRunSnapshot(
            id=run_id,
            conversation_id=conversation_id,
            assistant_message_id=assistant_message_id,
            user_request=user_request,
            status="running",
            tasks=[_new_task_snapshot(run_id, task) for task in tasks],
        )

    async def _add_tasks(
        self,
        run_id: uuid.UUID,
        tasks: Sequence[SupervisorTask],
    ) -> list[AgentTaskSnapshot]:
        if self._state_store is not None:
            return await self._state_store.add_tasks(run_id, tasks)
        return [_new_task_snapshot(run_id, task) for task in tasks]

    async def _update_run(
        self,
        run_id: uuid.UUID,
        status: AgentRunStatus,
    ) -> None:
        if self._state_store is not None:
            await self._state_store.update_run(run_id, status)

    async def _update_task(
        self,
        task_id: uuid.UUID,
        *,
        status: AgentTaskStatus,
        result: SpecialistResult | None = None,
        increment_attempt: bool = False,
        instruction: str | None = None,
    ) -> None:
        if self._state_store is not None:
            await self._state_store.update_task(
                task_id,
                status=status,
                result=result,
                increment_attempt=increment_attempt,
                instruction=instruction,
            )


class _RunEvents:
    def __init__(
        self,
        run_id: uuid.UUID,
        assistant_message_id: uuid.UUID,
        *,
        start_sequence: int,
        state_store: AgentStateStore | None,
    ) -> None:
        self._run_id = run_id
        self._assistant_message_id = assistant_message_id
        self._sequence = start_sequence
        self._state_store = state_store
        self._lock = asyncio.Lock()

    async def status(
        self,
        agent: AgentName,
        status: AgentTaskStatus,
        detail: str,
        *,
        task_id: uuid.UUID | None = None,
    ) -> AgentStatusEvent:
        async with self._lock:
            self._sequence += 1
            event = AgentStatusEvent(
                sequence=self._sequence,
                run_id=self._run_id,
                task_id=task_id,
                agent=agent,
                display_name=_AGENT_DISPLAY_NAMES[agent],
                status=status,
                detail=detail,
            )
            await self._record_safely(event)
            return event

    async def trace(
        self,
        agent: AgentName,
        action: str,
        status: str,
        title: str,
        *,
        detail: str | None = None,
        task_id: uuid.UUID | None = None,
        duration_ms: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> AgentTraceEvent:
        async with self._lock:
            self._sequence += 1
            event = AgentTraceEvent(
                sequence=self._sequence,
                run_id=self._run_id,
                task_id=task_id,
                agent=agent,
                action=action,
                status=status,
                title=title,
                detail=detail,
                duration_ms=duration_ms,
                data=data or {},
            )
            await self._record_safely(event)
            return event

    async def _record_safely(self, event: AgentDebugEvent) -> None:
        if self._state_store is None:
            return
        try:
            await self._state_store.record_event(event, self._assistant_message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Could not persist agent event run_id=%s sequence=%s exception_type=%s",
                self._run_id,
                event.sequence,
                type(exc).__name__,
            )


def _new_task_snapshot(
    run_id: uuid.UUID,
    task: SupervisorTask,
) -> AgentTaskSnapshot:
    return AgentTaskSnapshot(
        id=uuid.uuid4(),
        run_id=run_id,
        agent=task.agent,
        instruction=task.instruction,
        status="queued",
    )


def _resume_tasks(
    run: AgentRunSnapshot,
    decision: SupervisorDecision,
) -> list[AgentTaskSnapshot]:
    requested = {task.task_id: task for task in decision.tasks if task.task_id is not None}
    resumed = [
        task.model_copy(
            update={
                "instruction": requested[task.id].instruction,
            }
        )
        for task in run.tasks
        if task.id in requested and task.status in {"needs_input", "partial", "waiting"}
    ]
    if not resumed:
        raise ValueError("No resumable specialist tasks matched the supervisor decision.")
    return resumed


def _replace_tasks(
    existing: list[AgentTaskSnapshot],
    updates: list[AgentTaskSnapshot],
) -> list[AgentTaskSnapshot]:
    by_id = {task.id: task for task in updates}
    return [by_id.get(task.id, task) for task in existing]


def _collect_missing_fields(tasks: list[AgentTaskSnapshot]) -> list[MissingField]:
    unique: dict[str, MissingField] = {}
    for task in tasks:
        if task.status not in {"needs_input", "partial", "waiting"}:
            continue
        fields = task.result.missing_fields if task.result is not None else task.missing_fields
        for field in fields:
            unique.setdefault(field.field.casefold(), field)
    return list(unique.values())


def _clarification_message(
    missing: list[MissingField],
    tasks: list[AgentTaskSnapshot],
) -> str:
    completed = sum(task.status == "success" for task in tasks)
    prefix = "我已保留当前已完成的结果。"
    if completed:
        prefix = f"我已保留 {completed} 个已完成任务的结果。"
    lines = [prefix, "继续处理还需要你补充："]
    lines.extend(f"- {field.prompt}" for field in missing)
    return "\n".join(lines)


def _final_run_status(tasks: list[AgentTaskSnapshot]) -> AgentRunStatus:
    failures = sum(task.status == "failed" for task in tasks)
    if failures == len(tasks):
        return "failed"
    if failures:
        return "partial"
    return "completed"


def _trace_status(
    status: AgentTaskStatus,
) -> str:
    if status == "success":
        return "success"
    if status in {"partial", "needs_input"}:
        return "partial"
    if status == "cancelled":
        return "skipped"
    if status == "failed":
        return "failed"
    return "running"


def _running_detail(agent: SpecialistAgent) -> str:
    return {
        "itinerary": "正在处理行程与景点信息",
        "transport": "正在查询交通信息",
        "hotel": "正在查询酒店信息",
    }[agent]


def _latest_user_message(messages: list[ChatMessage]) -> str:
    return next(
        (message.content for message in reversed(messages) if message.role == "user"),
        "",
    )


__all__ = ["MultiAgentOrchestrator"]
