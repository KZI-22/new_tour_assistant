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


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    model_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

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

    __table_args__ = (Index("ix_conversations_updated_at", "updated_at"),)


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
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    plan_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    plan: Mapped[TravelPlan] = relationship(back_populates="versions")

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_travel_plan_versions_version"),
        UniqueConstraint("plan_id", "version", name="uq_travel_plan_versions_plan_version"),
        Index("ix_travel_plan_versions_plan_version", "plan_id", "version"),
    )
