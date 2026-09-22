"""isolated execution sessions and external blob storage

Revision ID: d5e8b3a4f901
Revises: c4b7a291e6d0
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e8b3a4f901"
down_revision: str | Sequence[str] | None = "c4b7a291e6d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("artifacts", "content", existing_type=sa.LargeBinary(), nullable=True)
    op.add_column("artifacts", sa.Column("storage_key", sa.String(length=128), nullable=True))

    op.create_table(
        "execution_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("runtime", sa.String(length=32), nullable=False),
        sa.Column("profile_id", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sandbox_ref", sa.String(length=128), nullable=True),
        sa.Column("source_action_id", sa.String(length=64), nullable=True),
        sa.Column("cell_seq", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('starting', 'ready', 'busy', 'lost', 'stopped')",
            name=op.f("ck_execution_sessions_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_execution_sessions_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_action_id"],
            ["actions.id"],
            name=op.f("fk_execution_sessions_source_action_id_actions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_sessions")),
        sa.UniqueConstraint("source_action_id", name="execution_session_source_action"),
    )
    op.create_index(
        "ix_execution_sessions_owner",
        "execution_sessions",
        ["tenant_id", "subject_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_execution_sessions_run_id"),
        "execution_sessions",
        ["run_id"],
        unique=False,
    )

    op.create_table(
        "executions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=True),
        sa.Column("generation", sa.String(length=64), nullable=False),
        sa.Column("cell_index", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'timed_out', "
            "'output_limit', 'artifact_limit', 'environment_lost')",
            name=op.f("ck_executions_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_executions_action_id_actions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["execution_sessions.id"],
            name=op.f("fk_executions_session_id_execution_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_executions")),
        sa.UniqueConstraint("action_id", name="execution_action"),
        sa.UniqueConstraint("session_id", "cell_index", name="execution_cell"),
    )
    op.create_index(op.f("ix_executions_session_id"), "executions", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_executions_session_id"), table_name="executions")
    op.drop_table("executions")
    op.drop_index(op.f("ix_execution_sessions_run_id"), table_name="execution_sessions")
    op.drop_index("ix_execution_sessions_owner", table_name="execution_sessions")
    op.drop_table("execution_sessions")
    op.drop_column("artifacts", "storage_key")
    op.alter_column("artifacts", "content", existing_type=sa.LargeBinary(), nullable=False)
