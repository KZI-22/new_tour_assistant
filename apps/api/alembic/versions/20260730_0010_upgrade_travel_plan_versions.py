"""Upgrade travel plan versions to persist canonical trip snapshots.

Revision ID: 20260730_0010
Revises: 20260722_0009
Create Date: 2026-07-30 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260730_0010"
down_revision: str | None = "20260722_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.add_column(
        "travel_plan_versions",
        sa.Column("parent_version_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "travel_plan_versions",
        sa.Column("assistant_message_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "travel_plan_versions",
        sa.Column("schema_version", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "travel_plan_versions",
        sa.Column("snapshot_json", jsonb, nullable=True),
    )
    op.add_column(
        "travel_plan_versions",
        sa.Column("presentation_context_json", jsonb, nullable=True),
    )
    op.add_column(
        "travel_plan_versions",
        sa.Column("narrative_json", jsonb, nullable=True),
    )
    op.add_column(
        "travel_plan_versions",
        sa.Column("rendered_markdown", sa.Text(), nullable=True),
    )
    op.add_column(
        "travel_plan_versions",
        sa.Column("user_instruction", sa.Text(), nullable=True),
    )
    op.add_column(
        "travel_plan_versions",
        sa.Column(
            "edit_operations_json",
            jsonb,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "travel_plan_versions",
        sa.Column(
            "invalidation_scope_json",
            jsonb,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "travel_plan_versions",
        sa.Column("validation_status", sa.String(length=20), nullable=True),
    )

    op.execute(
        sa.text(
            "UPDATE travel_plan_versions "
            "SET schema_version = 'legacy', "
            "snapshot_json = plan_json, "
            "validation_status = 'legacy'"
        )
    )
    op.alter_column(
        "travel_plan_versions",
        "schema_version",
        existing_type=sa.String(length=50),
        nullable=False,
    )
    op.alter_column(
        "travel_plan_versions",
        "snapshot_json",
        existing_type=jsonb,
        nullable=False,
    )
    op.alter_column(
        "travel_plan_versions",
        "validation_status",
        existing_type=sa.String(length=20),
        nullable=False,
    )

    op.create_foreign_key(
        "fk_travel_plan_versions_parent",
        "travel_plan_versions",
        "travel_plan_versions",
        ["parent_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_travel_plan_versions_assistant_message",
        "travel_plan_versions",
        "messages",
        ["assistant_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_travel_plan_versions_assistant_message",
        "travel_plan_versions",
        ["assistant_message_id"],
    )
    op.create_check_constraint(
        "ck_travel_plan_versions_validation_status",
        "travel_plan_versions",
        "validation_status IN ('valid', 'invalid', 'legacy')",
    )
    op.create_index(
        "ix_travel_plan_versions_parent",
        "travel_plan_versions",
        ["parent_version_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_travel_plan_versions_parent", table_name="travel_plan_versions")
    op.drop_constraint(
        "ck_travel_plan_versions_validation_status",
        "travel_plan_versions",
        type_="check",
    )
    op.drop_constraint(
        "uq_travel_plan_versions_assistant_message",
        "travel_plan_versions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_travel_plan_versions_assistant_message",
        "travel_plan_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_travel_plan_versions_parent",
        "travel_plan_versions",
        type_="foreignkey",
    )
    op.drop_column("travel_plan_versions", "validation_status")
    op.drop_column("travel_plan_versions", "invalidation_scope_json")
    op.drop_column("travel_plan_versions", "edit_operations_json")
    op.drop_column("travel_plan_versions", "user_instruction")
    op.drop_column("travel_plan_versions", "rendered_markdown")
    op.drop_column("travel_plan_versions", "narrative_json")
    op.drop_column("travel_plan_versions", "presentation_context_json")
    op.drop_column("travel_plan_versions", "snapshot_json")
    op.drop_column("travel_plan_versions", "schema_version")
    op.drop_column("travel_plan_versions", "assistant_message_id")
    op.drop_column("travel_plan_versions", "parent_version_id")
