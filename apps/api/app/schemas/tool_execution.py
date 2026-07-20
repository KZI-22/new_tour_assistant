from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    provider_error_code: str | None = Field(default=None, max_length=100)


class ToolResultMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    duration_ms: int = Field(ge=0)
    queried_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ToolResult(BaseModel):
    """Provider-neutral result that is safe to place in the model context."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    tool_name: str
    data: Any | None = None
    error: ToolError | None = None
    metadata: ToolResultMetadata

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.success:
            if self.error is not None:
                raise ValueError("successful tool results cannot contain an error")
        else:
            if self.data is not None:
                raise ValueError("failed tool results cannot contain data")
            if self.error is None:
                raise ValueError("failed tool results require an error")
        return self


class MessageDeltaEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["message_delta"] = "message_delta"
    delta: str


class ToolCallEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    tool_name: str
    display_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    success: bool
    summary: str
    duration_ms: int = Field(ge=0)
    error_code: str | None = None
    provider_error_code: str | None = None
    data_status: Literal["usable", "partial", "empty", "invalid"] | None = None
    provider_item_count: int | None = Field(default=None, ge=0)
    normalized_item_count: int | None = Field(default=None, ge=0)
    rejected_item_count: int | None = Field(default=None, ge=0)
    schema_version: str | None = None


class PlanningStageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["planning_stage"] = "planning_stage"
    stage: Literal[
        "understanding_request",
        "checking_requirements",
        "checking_xhs_login",
        "waiting_xhs_login",
        "collecting_transport",
        "collecting_hotels",
        "collecting_pois",
        "collecting_weather",
        "calculating_routes",
        "searching_xhs",
        "reading_xhs_posts",
        "generating_itinerary",
        "validating_itinerary",
        "revising_itinerary",
        "saving_itinerary",
        "finalizing",
    ]
    display_name: str
    status: Literal["running", "success", "partial", "failed", "skipped"]
    detail: str | None = None


class PlanningTraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["planning_trace"] = "planning_trace"
    sequence: int = Field(ge=1)
    step: Literal[
        "request_received",
        "route_selected",
        "requirements_extracted",
        "requirements_validated",
        "login_checked",
        "login_completed",
        "search_query_built",
        "search_results",
        "post_detail",
        "evidence_selected",
        "itinerary_generated",
        "validation_completed",
        "response_completed",
    ]
    title: str = Field(min_length=1, max_length=200)
    status: Literal["running", "success", "partial", "failed", "skipped"]
    detail: str | None = Field(default=None, max_length=500)
    duration_ms: int | None = Field(default=None, ge=0)
    data: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class XhsLoginRequiredEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["xhs_login_required"] = "xhs_login_required"
    login_id: str = Field(repr=False)
    expires_at: str
    message: str
    fallback_available: bool = False
    fallback_mode: Literal["map_weather"] | None = None


type ChatStreamEvent = (
    MessageDeltaEvent
    | ToolCallEvent
    | ToolResultEvent
    | PlanningStageEvent
    | PlanningTraceEvent
    | XhsLoginRequiredEvent
)


__all__ = [
    "ChatStreamEvent",
    "MessageDeltaEvent",
    "PlanningStageEvent",
    "PlanningTraceEvent",
    "ToolCallEvent",
    "ToolError",
    "ToolResult",
    "ToolResultEvent",
    "ToolResultMetadata",
    "XhsLoginRequiredEvent",
]
