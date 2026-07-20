from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.trip_planning import PlanningSource


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID | None = None
    model_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=100_000)
    planning_source: PlanningSource = "standard"


class ModelInfo(BaseModel):
    id: str
    display_name: str
    description: str
    provider: str
    available: bool
    unavailable_reason: str | None = None


class ModelListResponse(BaseModel):
    default_model: str | None
    models: list[ModelInfo]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
