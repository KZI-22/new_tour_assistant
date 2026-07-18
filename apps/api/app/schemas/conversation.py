from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ConversationMessageResponse(BaseModel):
    id: UUID
    sequence: int
    role: Literal["system", "user", "assistant"]
    content: str
    status: Literal["streaming", "completed", "failed", "interrupted"]
    created_at: datetime


class ConversationSummaryResponse(BaseModel):
    id: UUID
    title: str
    model_id: str
    created_at: datetime
    updated_at: datetime


class ConversationToolCallResponse(BaseModel):
    id: UUID
    assistant_message_id: UUID
    tool_call_id: str
    tool_name: str
    provider: str
    status: Literal["pending", "success", "failed"]
    result_summary: str
    error_code: str | None
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
