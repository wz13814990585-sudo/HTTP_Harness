"""MCP continuation, task, and RPC ledgers

Revision ID: f7a0d5c6b123
Revises: e6f9c4b5a012
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a0d5c6b123"
down_revision: str | Sequence[str] | None = "e6f9c4b5a012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_rpc_calls",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("integration_id", sa.String(length=128), nullable=False),
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_summary", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'complete', 'protocol_error', "
            "'input_required', 'task', 'outcome_unknown')",
            name=op.f("ck_mcp_rpc_calls_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_mcp_rpc_calls_action_id_actions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_rpc_calls")),
        sa.UniqueConstraint("action_id", "sequence", name="mcp_rpc_action_sequence"),
    )
    op.create_index(op.f("ix_mcp_rpc_calls_action_id"), "mcp_rpc_calls", ["action_id"])
    op.create_table(
        "mcp_continuations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("input_request_id", sa.String(length=64), nullable=False),
        sa.Column("integration_id", sa.String(length=128), nullable=False),
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("request_state", sa.Text(), nullable=True),
        sa.Column("input_requests", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'answered', 'resumed')",
            name=op.f("ck_mcp_continuations_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_mcp_continuations_action_id_actions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["input_request_id"],
            ["input_requests.id"],
            name=op.f("fk_mcp_continuations_input_request_id_input_requests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_continuations")),
        sa.UniqueConstraint("action_id", "sequence", name="mcp_continuation_sequence"),
        sa.UniqueConstraint("input_request_id", name="mcp_continuation_input_request"),
    )
    op.create_index(op.f("ix_mcp_continuations_action_id"), "mcp_continuations", ["action_id"])
    op.create_table(
        "mcp_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("integration_id", sa.String(length=128), nullable=False),
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_remote", sa.String(length=64), nullable=False),
        sa.Column("last_updated_at_remote", sa.String(length=64), nullable=False),
        sa.Column("ttl_ms", sa.BigInteger(), nullable=True),
        sa.Column("poll_interval_ms", sa.BigInteger(), nullable=True),
        sa.Column("input_requests", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("cancel_acknowledged", sa.Boolean(), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('working', 'input_required', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_mcp_tasks_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_mcp_tasks_action_id_actions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_tasks")),
        sa.UniqueConstraint("action_id", name="mcp_task_action"),
        sa.UniqueConstraint("integration_id", "profile", "task_id", name="mcp_task_remote"),
    )
    op.create_index(op.f("ix_mcp_tasks_action_id"), "mcp_tasks", ["action_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_mcp_tasks_action_id"), table_name="mcp_tasks")
    op.drop_table("mcp_tasks")
    op.drop_index(op.f("ix_mcp_continuations_action_id"), table_name="mcp_continuations")
    op.drop_table("mcp_continuations")
    op.drop_index(op.f("ix_mcp_rpc_calls_action_id"), table_name="mcp_rpc_calls")
    op.drop_table("mcp_rpc_calls")
