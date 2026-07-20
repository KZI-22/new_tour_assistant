"""Persist the user-selected planning evidence source on conversations.

Revision ID: 20260720_0007
Revises: 20260720_0006
Create Date: 2026-07-20 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260720_0007"
down_revision: str | None = "20260720_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "planning_source",
            sa.String(length=20),
            server_default="standard",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_conversations_planning_source",
        "conversations",
        "planning_source IN ('standard', 'xhs')",
    )
    op.alter_column("conversations", "planning_source", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "ck_conversations_planning_source",
        "conversations",
        type_="check",
    )
    op.drop_column("conversations", "planning_source")
