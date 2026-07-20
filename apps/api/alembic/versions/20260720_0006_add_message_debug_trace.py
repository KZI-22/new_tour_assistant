"""Persist sanitized planning debug traces on assistant messages.

Revision ID: 20260720_0006
Revises: 20260718_0005
Create Date: 2026-07-20 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260720_0006"
down_revision: str | None = "20260718_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "debug_trace_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.alter_column("messages", "debug_trace_json", server_default=None)


def downgrade() -> None:
    op.drop_column("messages", "debug_trace_json")
