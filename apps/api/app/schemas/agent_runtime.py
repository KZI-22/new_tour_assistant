from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SpecialistAgent = Literal["itinerary", "transport", "hotel"]
AgentName = Literal["supervisor", "itinerary", "transport", "hotel", "answer"]
AgentTaskStatus = Literal[
    "queued",
    "running",
    "waiting",
    "needs_input",
    "success",
    "partial",
    "failed",
    "cancelled",
]
AgentRunStatus = Literal[
    "running",
    "needs_input",
    "completed",
    "partial",
    "failed",
    "cancelled",
]


class MissingField(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=300)
    reason: str | None = Field(default=None, max_length=300)


class SupervisorTask(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent: SpecialistAgent
    instruction: str = Field(min_length=1, max_length=10_000)
    task_id: UUID | None = None


class SupervisorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["direct", "delegate", "resume"]
    tasks: list[SupervisorTask] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.mode == "direct" and self.tasks:
            raise ValueError("direct decisions cannot include specialist tasks")
        if self.mode in {"delegate", "resume"} and not self.tasks:
            raise ValueError(f"{self.mode} decisions require at least one specialist task")
        if self.mode == "delegate" and any(task.task_id is not None for task in self.tasks):
            raise ValueError("new specialist tasks cannot reference existing task ids")
        if self.mode == "resume" and any(task.task_id is None for task in self.tasks):
            raise ValueError("resumed specialist tasks require existing task ids")
        if len({task.agent for task in self.tasks}) != len(self.tasks):
            raise ValueError("a decision can include at most one task per specialist")
        return self


class SpecialistDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "partial", "needs_input", "failed"]
    summary: str = Field(min_length=1, max_length=2_000)
    data: Any | None = None
    missing_fields: list[MissingField] = Field(default_factory=list, max_length=20)
    error_code: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status in {"needs_input", "partial"} and not self.missing_fields:
            raise ValueError(f"{self.status} results require missing_fields")
        if self.status == "success" and self.missing_fields:
            raise ValueError("successful results cannot include missing_fields")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed results require an error_code")
        return self


class AgentToolArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    success: bool
    data: Any | None = None
    error_code: str | None = Field(default=None, max_length=100)
    duration_ms: int = Field(ge=0)


class SpecialistResult(SpecialistDecision):
    artifacts: list[AgentToolArtifact] = Field(default_factory=list)


class AgentTaskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    agent: SpecialistAgent
    instruction: str
    status: AgentTaskStatus
    missing_fields: list[MissingField] = Field(default_factory=list)
    result: SpecialistResult | None = None
    error_code: str | None = None
    attempt_count: int = Field(default=0, ge=0)


class AgentRunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    conversation_id: UUID
    assistant_message_id: UUID
    user_request: str
    status: AgentRunStatus
    tasks: list[AgentTaskSnapshot] = Field(default_factory=list)
    last_event_sequence: int = Field(default=0, ge=0)


class AgentStatusEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["agent_status"] = "agent_status"
    sequence: int = Field(ge=1)
    run_id: UUID
    task_id: UUID | None = None
    agent: AgentName
    display_name: str = Field(min_length=1, max_length=100)
    status: AgentTaskStatus
    detail: str | None = Field(default=None, max_length=500)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentTraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["agent_trace"] = "agent_trace"
    sequence: int = Field(ge=1)
    run_id: UUID
    task_id: UUID | None = None
    agent: AgentName
    action: str = Field(min_length=1, max_length=100)
    status: Literal["running", "success", "partial", "failed", "skipped"]
    title: str = Field(min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=500)
    duration_ms: int | None = Field(default=None, ge=0)
    data: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


type AgentDebugEvent = AgentStatusEvent | AgentTraceEvent


__all__ = [
    "AgentDebugEvent",
    "AgentName",
    "AgentRunSnapshot",
    "AgentRunStatus",
    "AgentStatusEvent",
    "AgentTaskSnapshot",
    "AgentTaskStatus",
    "AgentToolArtifact",
    "AgentTraceEvent",
    "MissingField",
    "SpecialistAgent",
    "SpecialistDecision",
    "SpecialistResult",
    "SupervisorDecision",
    "SupervisorTask",
]
