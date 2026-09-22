"""HTTP resource gateway and immutable artifacts.

Revision ID: 8f3c2d91a7b4
Revises: 1834feff7d1a
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f3c2d91a7b4"
down_revision: str | Sequence[str] | None = "1834feff7d1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("actions", sa.Column("capability_id", sa.String(length=128), nullable=True))
    op.add_column("actions", sa.Column("bound_operation", sa.JSON(), nullable=True))
    op.add_column("actions", sa.Column("result", sa.JSON(), nullable=True))
    op.execute("UPDATE actions SET capability_id = 'legacy', bound_operation = '{}'::json")
    op.alter_column("actions", "capability_id", nullable=False)
    op.alter_column("actions", "bound_operation", nullable=False)

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("source_action_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_action_id"],
            ["actions.id"],
            name=op.f("fk_artifacts_source_action_id_actions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
    )
    op.create_index(
        "ix_artifacts_owner_created",
        "artifacts",
        ["tenant_id", "subject_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "http_exchanges",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("transport_kind", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_http_exchanges_action_id_actions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_http_exchanges")),
    )
    op.create_index(
        op.f("ix_http_exchanges_action_id"),
        "http_exchanges",
        ["action_id"],
        unique=False,
    )
    op.create_table(
        "workspace_entries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("logical_path", sa.String(length=1024), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(length=160), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_workspace_entries_artifact_id_artifacts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_entries")),
        sa.UniqueConstraint(
            "tenant_id",
            "subject_id",
            "workspace_id",
            "logical_path",
            name="workspace_path",
        ),
    )


def downgrade() -> None:
    op.drop_table("workspace_entries")
    op.drop_index(op.f("ix_http_exchanges_action_id"), table_name="http_exchanges")
    op.drop_table("http_exchanges")
    op.drop_index("ix_artifacts_owner_created", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_column("actions", "result")
    op.drop_column("actions", "bound_operation")
    op.drop_column("actions", "capability_id")
