from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ToolCallLog

ToolCallStatus = Literal["success", "failed"]


@dataclass(frozen=True, slots=True)
class ToolCallLogEntry:
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    tool_call_id: str
    tool_name: str
    provider: str
    arguments: dict[str, Any]
    status: ToolCallStatus
    result_summary: str
    error_code: str | None
    duration_ms: int


class ToolCallLogWriter(Protocol):
    async def record(self, entry: ToolCallLogEntry) -> None: ...


class ToolCallLogService:
    """Persist one sanitized, summary-only audit row per completed tool call."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, entry: ToolCallLogEntry) -> None:
        async with self._session_factory() as session, session.begin():
            session.add(
                ToolCallLog(
                    conversation_id=entry.conversation_id,
                    assistant_message_id=entry.assistant_message_id,
                    tool_call_id=entry.tool_call_id,
                    tool_name=entry.tool_name,
                    provider=entry.provider,
                    arguments_json=entry.arguments,
                    status=entry.status,
                    result_summary=entry.result_summary,
                    error_code=entry.error_code,
                    duration_ms=entry.duration_ms,
                )
            )


__all__ = [
    "ToolCallLogEntry",
    "ToolCallLogService",
    "ToolCallLogWriter",
    "ToolCallStatus",
]
