from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

_JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    display_name: Mapped[str | None] = mapped_column(String(100))
    phone_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status"),
        Index("ix_users_status", "status"),
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    user_agent: Mapped[str | None] = mapped_column(String(500))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (Index("ix_user_sessions_user_state", "user_id", "revoked_at", "expires_at"),)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    model_id: Mapped[str] = mapped_column(String(100), nullable=False)
    planning_source: Mapped[str] = mapped_column(String(20), nullable=False, default="standard")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    travel_plan: Mapped[TravelPlan | None] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    agent_runs: Mapped[list[AgentRun]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "planning_source IN ('standard', 'xhs')",
            name="ck_conversations_planning_source",
        ),
        Index("ix_conversations_user_updated_at", "user_id", "updated_at"),
        Index("ix_conversations_updated_at", "updated_at"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    debug_trace_json: Mapped[list[dict[str, Any]]] = mapped_column(
        _JSON_DOCUMENT,
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        CheckConstraint("role IN ('system', 'user', 'assistant')", name="ck_messages_role"),
        CheckConstraint(
            "status IN ('streaming', 'completed', 'failed', 'interrupted')",
            name="ck_messages_status",
        ),
        UniqueConstraint("conversation_id", "sequence", name="uq_messages_conversation_seq"),
        Index("ix_messages_conversation_sequence", "conversation_id", "sequence"),
    )


class ToolCallLog(Base):
    __tablename__ = "tool_call_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    assistant_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    agent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_task_runs.id", ondelete="SET NULL")
    )
    agent_name: Mapped[str | None] = mapped_column(String(30))
    process_status: Mapped[str | None] = mapped_column(String(20))
    process_return_code: Mapped[int | None] = mapped_column(Integer)
    provider_status: Mapped[str | None] = mapped_column(String(20))
    parse_status: Mapped[str | None] = mapped_column(String(20))
    business_status: Mapped[str | None] = mapped_column(String(20))
    tool_call_id: Mapped[str] = mapped_column(String(200), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    result_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    provider_error_code: Mapped[str | None] = mapped_column(String(100))
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    data_status: Mapped[str | None] = mapped_column(String(20))
    provider_item_count: Mapped[int | None] = mapped_column(Integer)
    normalized_item_count: Mapped[int | None] = mapped_column(Integer)
    rejected_item_count: Mapped[int | None] = mapped_column(Integer)
    schema_version: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'success', 'failed')",
            name="ck_tool_call_logs_status",
        ),
        CheckConstraint(
            "data_status IS NULL OR data_status IN ('usable', 'partial', 'empty', 'invalid')",
            name="ck_tool_call_logs_data_status",
        ),
        Index("ix_tool_call_logs_conversation_created", "conversation_id", "created_at"),
        Index("ix_tool_call_logs_assistant_message", "assistant_message_id"),
        Index("ix_tool_call_logs_agent_run", "agent_run_id"),
        Index("ix_tool_call_logs_agent_task", "agent_task_id"),
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    assistant_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    user_request: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[Conversation] = relationship(back_populates="agent_runs")
    tasks: Mapped[list[AgentTaskRun]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AgentTaskRun.created_at",
    )
    events: Mapped[list[AgentRuntimeEvent]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AgentRuntimeEvent.sequence",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'needs_input', 'completed', 'partial', 'failed', 'cancelled')",
            name="ck_agent_runs_status",
        ),
        Index("ix_agent_runs_conversation_updated", "conversation_id", "updated_at"),
        Index("ix_agent_runs_conversation_status", "conversation_id", "status"),
    )


class AgentTaskRun(Base):
    __tablename__ = "agent_task_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(30), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    missing_fields_json: Mapped[list[dict[str, Any]]] = mapped_column(
        _JSON_DOCUMENT,
        nullable=False,
        default=list,
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT)
    error_code: Mapped[str | None] = mapped_column(String(100))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[AgentRun] = relationship(back_populates="tasks")
    events: Mapped[list[AgentRuntimeEvent]] = relationship(
        back_populates="task",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "agent_name IN ('itinerary', 'transport', 'hotel')",
            name="ck_agent_task_runs_agent_name",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting', 'needs_input', "
            "'success', 'partial', 'failed', 'cancelled')",
            name="ck_agent_task_runs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_agent_task_runs_attempt_count"),
        UniqueConstraint("run_id", "agent_name", name="uq_agent_task_runs_run_agent"),
        Index("ix_agent_task_runs_run_status", "run_id", "status"),
    )


class AgentRuntimeEvent(Base):
    __tablename__ = "agent_runtime_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_task_runs.id", ondelete="CASCADE")
    )
    assistant_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(30), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500))
    data_json: Mapped[dict[str, Any]] = mapped_column(
        _JSON_DOCUMENT,
        nullable=False,
        default=dict,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[AgentRun] = relationship(back_populates="events")
    task: Mapped[AgentTaskRun | None] = relationship(back_populates="events")

    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_agent_runtime_events_sequence"),
        UniqueConstraint("run_id", "sequence", name="uq_agent_runtime_events_run_sequence"),
        Index("ix_agent_runtime_events_run_sequence", "run_id", "sequence"),
        Index("ix_agent_runtime_events_task", "task_id"),
    )


class TravelPlan(Base):
    __tablename__ = "travel_plans"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="travel_plan")
    versions: Mapped[list[TravelPlanVersion]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TravelPlanVersion.version",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_travel_plans_status",
        ),
        CheckConstraint("current_version >= 0", name="ck_travel_plans_current_version"),
        Index("ix_travel_plans_conversation_status", "conversation_id", "status"),
    )


class TravelPlanVersion(Base):
    __tablename__ = "travel_plan_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("travel_plans.id", ondelete="CASCADE"), nullable=False
    )
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("travel_plan_versions.id", ondelete="SET NULL")
    )
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="trip_plan.v1",
    )
    request_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    plan_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    presentation_context_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT)
    narrative_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT)
    rendered_markdown: Mapped[str | None] = mapped_column(Text)
    user_instruction: Mapped[str | None] = mapped_column(Text)
    edit_operations_json: Mapped[list[dict[str, Any]]] = mapped_column(
        _JSON_DOCUMENT,
        nullable=False,
        default=list,
    )
    invalidation_scope_json: Mapped[dict[str, Any]] = mapped_column(
        _JSON_DOCUMENT,
        nullable=False,
        default=dict,
    )
    validation_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="valid",
    )
    change_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    plan: Mapped[TravelPlan] = relationship(back_populates="versions")

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_travel_plan_versions_version"),
        CheckConstraint(
            "validation_status IN ('valid', 'invalid', 'legacy')",
            name="ck_travel_plan_versions_validation_status",
        ),
        UniqueConstraint("plan_id", "version", name="uq_travel_plan_versions_plan_version"),
        UniqueConstraint(
            "assistant_message_id",
            name="uq_travel_plan_versions_assistant_message",
        ),
        Index("ix_travel_plan_versions_plan_version", "plan_id", "version"),
        Index("ix_travel_plan_versions_parent", "parent_version_id"),
    )
