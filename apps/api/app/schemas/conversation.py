from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.agent_runtime import AgentDebugEvent
from app.schemas.tool_execution import PlanningTraceEvent
from app.schemas.trip_planning import PlanningSource


class ConversationMessageResponse(BaseModel):
    id: UUID
    sequence: int
    role: Literal["system", "user", "assistant"]
    content: str
    status: Literal["streaming", "completed", "failed", "interrupted"]
    debug_trace: list[PlanningTraceEvent | AgentDebugEvent] = Field(default_factory=list)
    created_at: datetime


class ConversationSummaryResponse(BaseModel):
    id: UUID
    title: str
    model_id: str
    planning_source: PlanningSource
    created_at: datetime
    updated_at: datetime


class ConversationToolCallResponse(BaseModel):
    id: UUID
    assistant_message_id: UUID
    agent_run_id: UUID | None = None
    agent_task_id: UUID | None = None
    agent_name: str | None = None
    process_status: str | None = None
    process_return_code: int | None = None
    provider_status: str | None = None
    parse_status: str | None = None
    business_status: str | None = None
    tool_call_id: str
    tool_name: str
    provider: str
    status: Literal["pending", "success", "failed"]
    result_summary: str
    error_code: str | None
    provider_error_code: str | None
    duration_ms: int
    data_status: Literal["usable", "partial", "empty", "invalid"] | None = None
    provider_item_count: int | None = None
    normalized_item_count: int | None = None
    rejected_item_count: int | None = None
    schema_version: str | None = None
    created_at: datetime


class ConversationDetailResponse(ConversationSummaryResponse):
    messages: list[ConversationMessageResponse]
    tool_calls: list[ConversationToolCallResponse] = Field(default_factory=list)
