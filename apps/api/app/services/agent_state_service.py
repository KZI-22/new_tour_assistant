from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.db.models import AgentRun, AgentRuntimeEvent, AgentTaskRun
from app.schemas.agent_runtime import (
    AgentDebugEvent,
    AgentRunSnapshot,
    AgentRunStatus,
    AgentTaskSnapshot,
    AgentTaskStatus,
    AgentTraceEvent,
    SpecialistResult,
    SupervisorTask,
)


class AgentStateStore(Protocol):
    async def create_run(
        self,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        assistant_message_id: uuid.UUID,
        user_request: str,
        tasks: Sequence[SupervisorTask],
    ) -> AgentRunSnapshot: ...

    async def add_tasks(
        self,
        run_id: uuid.UUID,
        tasks: Sequence[SupervisorTask],
    ) -> list[AgentTaskSnapshot]: ...

    async def find_pending_run(
        self,
        conversation_id: uuid.UUID,
    ) -> AgentRunSnapshot | None: ...

    async def update_run(self, run_id: uuid.UUID, status: AgentRunStatus) -> None: ...

    async def update_task(
        self,
        task_id: uuid.UUID,
        *,
        status: AgentTaskStatus,
        result: SpecialistResult | None = None,
        increment_attempt: bool = False,
        instruction: str | None = None,
    ) -> None: ...

    async def record_event(
        self,
        event: AgentDebugEvent,
        assistant_message_id: uuid.UUID,
    ) -> None: ...


class AgentStateService:
    """Persist supervisor-owned run state without exposing it to sibling agents."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_run(
        self,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        assistant_message_id: uuid.UUID,
        user_request: str,
        tasks: Sequence[SupervisorTask],
    ) -> AgentRunSnapshot:
        now = datetime.now(UTC)
        run = AgentRun(
            id=run_id,
            conversation_id=conversation_id,
            assistant_message_id=assistant_message_id,
            user_request=user_request,
            status="running",
            created_at=now,
            updated_at=now,
        )
        run.tasks = [
            AgentTaskRun(
                agent_name=task.agent,
                instruction=task.instruction,
                status="queued",
                created_at=now,
                updated_at=now,
            )
            for task in tasks
        ]
        async with self._session_factory() as session, session.begin():
            session.add(run)
            await session.flush()
            snapshot = _run_snapshot(run)
        return snapshot

    async def add_tasks(
        self,
        run_id: uuid.UUID,
        tasks: Sequence[SupervisorTask],
    ) -> list[AgentTaskSnapshot]:
        now = datetime.now(UTC)
        records = [
            AgentTaskRun(
                run_id=run_id,
                agent_name=task.agent,
                instruction=task.instruction,
                status="queued",
                created_at=now,
                updated_at=now,
            )
            for task in tasks
        ]
        async with self._session_factory() as session, session.begin():
            session.add_all(records)
            await session.flush()
            snapshots = [_task_snapshot(record) for record in records]
        return snapshots

    async def find_pending_run(
        self,
        conversation_id: uuid.UUID,
    ) -> AgentRunSnapshot | None:
        async with self._session_factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.conversation_id == conversation_id,
                    AgentRun.status == "needs_input",
                )
                .options(selectinload(AgentRun.tasks))
                .order_by(AgentRun.updated_at.desc())
                .limit(1)
            )
            if run is None:
                return None
            last_sequence = await session.scalar(
                select(func.max(AgentRuntimeEvent.sequence)).where(
                    AgentRuntimeEvent.run_id == run.id
                )
            )
            return _run_snapshot(run, last_event_sequence=last_sequence or 0)

    async def update_run(self, run_id: uuid.UUID, status: AgentRunStatus) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            run = await session.get(AgentRun, run_id)
            if run is None:
                return
            run.status = status
            run.updated_at = now
            run.completed_at = (
                now if status in {"completed", "partial", "failed", "cancelled"} else None
            )

    async def update_task(
        self,
        task_id: uuid.UUID,
        *,
        status: AgentTaskStatus,
        result: SpecialistResult | None = None,
        increment_attempt: bool = False,
        instruction: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            task = await session.get(AgentTaskRun, task_id)
            if task is None:
                return
            task.status = status
            task.updated_at = now
            if increment_attempt:
                task.attempt_count += 1
            if instruction is not None:
                task.instruction = instruction
            if result is not None:
                task.result_json = result.model_dump(mode="json")
                task.missing_fields_json = [
                    field.model_dump(mode="json") for field in result.missing_fields
                ]
                task.error_code = result.error_code
            run = await session.get(AgentRun, task.run_id)
            if run is not None:
                run.updated_at = now

    async def record_event(
        self,
        event: AgentDebugEvent,
        assistant_message_id: uuid.UUID,
    ) -> None:
        if isinstance(event, AgentTraceEvent):
            event_type = event.action
            status = event.status
            detail = event.detail
            data = event.data
        else:
            event_type = "agent_status"
            status = event.status
            detail = event.detail
            data = {"display_name": event.display_name}
        async with self._session_factory() as session, session.begin():
            session.add(
                AgentRuntimeEvent(
                    run_id=event.run_id,
                    task_id=event.task_id,
                    assistant_message_id=assistant_message_id,
                    sequence=event.sequence,
                    agent_name=event.agent,
                    event_type=event_type,
                    status=status,
                    detail=detail,
                    data_json=data,
                    occurred_at=event.occurred_at,
                )
            )


def _run_snapshot(
    run: AgentRun,
    *,
    last_event_sequence: int = 0,
) -> AgentRunSnapshot:
    return AgentRunSnapshot(
        id=run.id,
        conversation_id=run.conversation_id,
        assistant_message_id=run.assistant_message_id,
        user_request=run.user_request,
        status=run.status,
        tasks=[_task_snapshot(task) for task in run.tasks],
        last_event_sequence=last_event_sequence,
    )


def _task_snapshot(task: AgentTaskRun) -> AgentTaskSnapshot:
    return AgentTaskSnapshot(
        id=task.id,
        run_id=task.run_id,
        agent=task.agent_name,
        instruction=task.instruction,
        status=task.status,
        missing_fields=task.missing_fields_json or [],
        result=task.result_json,
        error_code=task.error_code,
        attempt_count=task.attempt_count,
    )


__all__ = ["AgentStateService", "AgentStateStore"]
