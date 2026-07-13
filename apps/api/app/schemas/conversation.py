from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


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


class ConversationDetailResponse(ConversationSummaryResponse):
    messages: list[ConversationMessageResponse]

