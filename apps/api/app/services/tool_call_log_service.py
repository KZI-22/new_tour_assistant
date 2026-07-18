from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sqlalchemy import update
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
    provider_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallQualityUpdate:
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    tool_call_id: str
    data_status: Literal["usable", "partial", "empty", "invalid"]
    provider_item_count: int
    normalized_item_count: int
    rejected_item_count: int
    schema_version: str
    result_summary: str
    error_code: str | None


class ToolCallLogWriter(Protocol):
    async def record(self, entry: ToolCallLogEntry) -> None: ...

    async def update_data_quality(self, quality: ToolCallQualityUpdate) -> None: ...


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
                    provider_error_code=entry.provider_error_code,
                    duration_ms=entry.duration_ms,
                )
            )

    async def update_data_quality(self, quality: ToolCallQualityUpdate) -> None:
        """Attach normalized-data quality without persisting provider payloads."""

        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ToolCallLog)
                .where(
                    ToolCallLog.conversation_id == quality.conversation_id,
                    ToolCallLog.assistant_message_id == quality.assistant_message_id,
                    ToolCallLog.tool_call_id == quality.tool_call_id,
                )
                .values(
                    data_status=quality.data_status,
                    provider_item_count=quality.provider_item_count,
                    normalized_item_count=quality.normalized_item_count,
                    rejected_item_count=quality.rejected_item_count,
                    schema_version=quality.schema_version,
                    result_summary=quality.result_summary,
                    error_code=quality.error_code,
                )
            )


__all__ = [
    "ToolCallLogEntry",
    "ToolCallQualityUpdate",
    "ToolCallLogService",
    "ToolCallLogWriter",
    "ToolCallStatus",
]
