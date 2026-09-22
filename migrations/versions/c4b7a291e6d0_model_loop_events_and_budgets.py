"""model loop, event continuation, completion, and budget reservations

Revision ID: c4b7a291e6d0
Revises: 8f3c2d91a7b4
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4b7a291e6d0"
down_revision: str | Sequence[str] | None = "8f3c2d91a7b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("final_answer", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("result_artifact_ids", sa.JSON(), nullable=True))
    op.add_column("runs", sa.Column("failure", sa.JSON(), nullable=True))
    op.execute("UPDATE runs SET result_artifact_ids = '[]'::json")
    op.alter_column("runs", "result_artifact_ids", nullable=False)

    op.add_column("model_calls", sa.Column("request_payload", sa.JSON(), nullable=True))
    op.add_column("model_calls", sa.Column("response_payload", sa.JSON(), nullable=True))
    op.add_column("model_calls", sa.Column("parsed_output", sa.JSON(), nullable=True))
    op.add_column("model_calls", sa.Column("response_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "model_calls", sa.Column("provider_request_id", sa.String(length=256), nullable=True)
    )
    op.add_column(
        "model_calls",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "model_calls",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.execute("UPDATE model_calls SET request_payload = '{}'::json")
    op.alter_column("model_calls", "request_payload", nullable=False)

    op.add_column("events", sa.Column("action_id", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        op.f("fk_events_action_id_actions"),
        "events",
        "actions",
        ["action_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "model_call_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("model_call_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_request_id", sa.String(length=256), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["model_call_id"],
            ["model_calls.id"],
            name=op.f("fk_model_call_attempts_model_call_id_model_calls"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_call_attempts")),
        sa.UniqueConstraint("model_call_id", "attempt_no", name="model_call_attempt"),
    )
    op.create_index(
        op.f("ix_model_call_attempts_model_call_id"),
        "model_call_attempts",
        ["model_call_id"],
        unique=False,
    )
    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("reservation_key", sa.String(length=256), nullable=False),
        sa.Column("dimension", sa.String(length=32), nullable=False),
        sa.Column("reserved_amount", sa.BigInteger(), nullable=False),
        sa.Column("actual_amount", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
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
            "status IN ('reserved', 'settled', 'released')",
            name=op.f("ck_budget_reservations_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_budget_reservations_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_reservations")),
        sa.UniqueConstraint("run_id", "reservation_key", name="budget_reservation_key"),
    )
    op.create_index(
        op.f("ix_budget_reservations_run_id"),
        "budget_reservations",
        ["run_id"],
        unique=False,
    )
    op.create_table(
        "context_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_context_snapshots_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_snapshots")),
        sa.UniqueConstraint("run_id", "turn_id", name="context_turn"),
    )
    op.create_index(
        op.f("ix_context_snapshots_run_id"),
        "context_snapshots",
        ["run_id"],
        unique=False,
    )
    op.create_table(
        "completion_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("model_call_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("candidate", sa.JSON(), nullable=False),
        sa.Column("missing_evidence", sa.JSON(), nullable=False),
        sa.Column("acceptance_results", sa.JSON(), nullable=False),
        sa.Column("regression_results", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["model_call_id"],
            ["model_calls.id"],
            name=op.f("fk_completion_records_model_call_id_model_calls"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_completion_records_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_completion_records")),
    )
    op.create_index(
        op.f("ix_completion_records_run_id"),
        "completion_records",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_completion_records_run_id"), table_name="completion_records")
    op.drop_table("completion_records")
    op.drop_index(op.f("ix_context_snapshots_run_id"), table_name="context_snapshots")
    op.drop_table("context_snapshots")
    op.drop_index(op.f("ix_budget_reservations_run_id"), table_name="budget_reservations")
    op.drop_table("budget_reservations")
    op.drop_index(op.f("ix_model_call_attempts_model_call_id"), table_name="model_call_attempts")
    op.drop_table("model_call_attempts")
    op.drop_constraint(op.f("fk_events_action_id_actions"), "events", type_="foreignkey")
    op.drop_column("events", "action_id")
    op.drop_column("model_calls", "updated_at")
    op.drop_column("model_calls", "created_at")
    op.drop_column("model_calls", "provider_request_id")
    op.drop_column("model_calls", "response_hash")
    op.drop_column("model_calls", "parsed_output")
    op.drop_column("model_calls", "response_payload")
    op.drop_column("model_calls", "request_payload")
    op.drop_column("runs", "failure")
    op.drop_column("runs", "result_artifact_ids")
    op.drop_column("runs", "final_answer")
