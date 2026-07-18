"""Persist sanitized provider error codes on tool call logs.

Revision ID: 20260718_0005
Revises: 20260718_0004
Create Date: 2026-07-18 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0005"
down_revision: str | None = "20260718_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_call_logs",
        sa.Column("provider_error_code", sa.String(length=100)),
    )


def downgrade() -> None:
    op.drop_column("tool_call_logs", "provider_error_code")
