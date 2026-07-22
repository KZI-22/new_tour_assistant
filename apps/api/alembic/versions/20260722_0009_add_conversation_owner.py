"""Bind every conversation to a user account.

Revision ID: 20260722_0009
Revises: 20260722_0008
Create Date: 2026-07-22 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260722_0009"
down_revision: str | None = "20260722_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    conversation_count = bind.scalar(sa.text("SELECT count(*) FROM conversations"))
    if conversation_count:
        raise RuntimeError(
            "Cannot add a non-null conversation owner while conversations exist. "
            "Explicitly reset development data or backfill owners before retrying."
        )
    op.add_column("conversations", sa.Column("user_id", sa.Uuid(), nullable=False))
    op.create_foreign_key(
        "fk_conversations_user_id_users",
        "conversations",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_conversations_user_updated_at",
        "conversations",
        ["user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_user_updated_at", table_name="conversations")
    op.drop_constraint(
        "fk_conversations_user_id_users",
        "conversations",
        type_="foreignkey",
    )
    op.drop_column("conversations", "user_id")
