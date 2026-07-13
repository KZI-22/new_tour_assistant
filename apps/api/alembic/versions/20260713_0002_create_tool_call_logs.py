"""Create the sanitized tool call audit table.

Revision ID: 20260713_0002
Revises: 20260713_0001
Create Date: 2026-07-13 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260713_0002"
down_revision: str | None = "20260713_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_call_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("assistant_message_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.String(length=200), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result_summary", sa.String(length=500), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'success', 'failed')",
            name="ck_tool_call_logs_status",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"],
            ["messages.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tool_call_logs_assistant_message",
        "tool_call_logs",
        ["assistant_message_id"],
    )
    op.create_index(
        "ix_tool_call_logs_conversation_created",
        "tool_call_logs",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_call_logs_conversation_created", table_name="tool_call_logs")
    op.drop_index("ix_tool_call_logs_assistant_message", table_name="tool_call_logs")
    op.drop_table("tool_call_logs")
