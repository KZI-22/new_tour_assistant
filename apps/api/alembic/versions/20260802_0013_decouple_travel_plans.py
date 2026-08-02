"""Decouple travel plans from assistant conversations.

Revision ID: 20260802_0013
Revises: 20260731_0012
Create Date: 2026-08-02 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0013"
down_revision: str | None = "20260731_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("travel_plans", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE travel_plans AS plan "
            "SET user_id = conversation.user_id "
            "FROM conversations AS conversation "
            "WHERE plan.conversation_id = conversation.id"
        )
    )
    op.alter_column("travel_plans", "user_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "fk_travel_plans_user",
        "travel_plans",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_travel_plans_user_updated_at",
        "travel_plans",
        ["user_id", "updated_at"],
    )

    op.drop_constraint(
        "travel_plans_conversation_id_fkey",
        "travel_plans",
        type_="foreignkey",
    )
    op.alter_column(
        "travel_plans",
        "conversation_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_travel_plans_conversation",
        "travel_plans",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM travel_plans WHERE conversation_id IS NULL"))
    op.drop_constraint(
        "fk_travel_plans_conversation",
        "travel_plans",
        type_="foreignkey",
    )
    op.alter_column(
        "travel_plans",
        "conversation_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.create_foreign_key(
        "travel_plans_conversation_id_fkey",
        "travel_plans",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_index("ix_travel_plans_user_updated_at", table_name="travel_plans")
    op.drop_constraint("fk_travel_plans_user", "travel_plans", type_="foreignkey")
    op.drop_column("travel_plans", "user_id")
