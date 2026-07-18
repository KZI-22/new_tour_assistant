"""Add normalized-data quality fields to tool call logs.

Revision ID: 20260718_0004
Revises: 20260714_0003
Create Date: 2026-07-18 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0004"
down_revision: str | None = "20260714_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tool_call_logs", sa.Column("data_status", sa.String(length=20)))
    op.add_column("tool_call_logs", sa.Column("provider_item_count", sa.Integer()))
    op.add_column("tool_call_logs", sa.Column("normalized_item_count", sa.Integer()))
    op.add_column("tool_call_logs", sa.Column("rejected_item_count", sa.Integer()))
    op.add_column("tool_call_logs", sa.Column("schema_version", sa.String(length=100)))
    op.create_check_constraint(
        "ck_tool_call_logs_data_status",
        "tool_call_logs",
        "data_status IS NULL OR data_status IN ('usable', 'partial', 'empty', 'invalid')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_tool_call_logs_data_status",
        "tool_call_logs",
        type_="check",
    )
    op.drop_column("tool_call_logs", "schema_version")
    op.drop_column("tool_call_logs", "rejected_item_count")
    op.drop_column("tool_call_logs", "normalized_item_count")
    op.drop_column("tool_call_logs", "provider_item_count")
    op.drop_column("tool_call_logs", "data_status")
