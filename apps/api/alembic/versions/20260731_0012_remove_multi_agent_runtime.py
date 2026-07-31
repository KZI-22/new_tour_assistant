"""Remove the obsolete multi-agent runtime state.

Revision ID: 20260731_0012
Revises: 20260730_0011
Create Date: 2026-07-31 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260731_0012"
down_revision: str | None = "20260730_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE messages
        SET debug_trace_json = COALESCE(
            (
                SELECT jsonb_agg(item)
                FROM jsonb_array_elements(debug_trace_json) AS item
                WHERE item->>'type' NOT IN ('agent_status', 'agent_trace')
            ),
            '[]'::jsonb
        )
        """
    )

    op.drop_index("ix_tool_call_logs_agent_task", table_name="tool_call_logs")
    op.drop_index("ix_tool_call_logs_agent_run", table_name="tool_call_logs")
    op.drop_constraint("fk_tool_call_logs_agent_task", "tool_call_logs", type_="foreignkey")
    op.drop_constraint("fk_tool_call_logs_agent_run", "tool_call_logs", type_="foreignkey")
    op.drop_column("tool_call_logs", "agent_name")
    op.drop_column("tool_call_logs", "agent_task_id")
    op.drop_column("tool_call_logs", "agent_run_id")

    op.drop_table("agent_runtime_events")
    op.drop_table("agent_task_runs")
    op.drop_table("agent_runs")


def downgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("assistant_message_id", sa.Uuid(), nullable=False),
        sa.Column("user_request", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'needs_input', 'completed', 'partial', 'failed', 'cancelled')",
            name="ck_agent_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"],
            ["messages.id"],
            name="fk_agent_runs_assistant_message",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_agent_runs_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assistant_message_id", name="uq_agent_runs_assistant_message"),
    )
    op.create_index(
        "ix_agent_runs_conversation_updated",
        "agent_runs",
        ["conversation_id", "updated_at"],
    )
    op.create_index(
        "ix_agent_runs_conversation_status",
        "agent_runs",
        ["conversation_id", "status"],
    )

    op.create_table(
        "agent_task_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_name", sa.String(length=30), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "missing_fields_json",
            jsonb,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("result_json", jsonb, nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "agent_name IN ('itinerary', 'transport', 'hotel')",
            name="ck_agent_task_runs_agent_name",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'waiting', 'needs_input', "
            "'success', 'partial', 'failed', 'cancelled')",
            name="ck_agent_task_runs_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_agent_task_runs_attempt_count",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name="fk_agent_task_runs_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "agent_name",
            name="uq_agent_task_runs_run_agent",
        ),
    )
    op.create_index(
        "ix_agent_task_runs_run_status",
        "agent_task_runs",
        ["run_id", "status"],
    )

    op.create_table(
        "agent_runtime_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("assistant_message_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("agent_name", sa.String(length=30), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column(
            "data_json",
            jsonb,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_agent_runtime_events_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"],
            ["messages.id"],
            name="fk_agent_runtime_events_assistant_message",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name="fk_agent_runtime_events_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_task_runs.id"],
            name="fk_agent_runtime_events_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "sequence",
            name="uq_agent_runtime_events_run_sequence",
        ),
    )
    op.create_index(
        "ix_agent_runtime_events_run_sequence",
        "agent_runtime_events",
        ["run_id", "sequence"],
    )
    op.create_index(
        "ix_agent_runtime_events_task",
        "agent_runtime_events",
        ["task_id"],
    )

    op.add_column(
        "tool_call_logs",
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "tool_call_logs",
        sa.Column("agent_task_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "tool_call_logs",
        sa.Column("agent_name", sa.String(length=30), nullable=True),
    )
    op.create_foreign_key(
        "fk_tool_call_logs_agent_run",
        "tool_call_logs",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_tool_call_logs_agent_task",
        "tool_call_logs",
        "agent_task_runs",
        ["agent_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_tool_call_logs_agent_run",
        "tool_call_logs",
        ["agent_run_id"],
    )
    op.create_index(
        "ix_tool_call_logs_agent_task",
        "tool_call_logs",
        ["agent_task_id"],
    )
