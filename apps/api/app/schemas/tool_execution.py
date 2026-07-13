from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False


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


type ChatStreamEvent = MessageDeltaEvent | ToolCallEvent | ToolResultEvent


__all__ = [
    "ChatStreamEvent",
    "MessageDeltaEvent",
    "ToolCallEvent",
    "ToolError",
    "ToolResult",
    "ToolResultEvent",
    "ToolResultMetadata",
]
