"""reliability approvals, recovery evidence, fencing, and checkpoints

Revision ID: e6f9c4b5a012
Revises: d5e8b3a4f901
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f9c4b5a012"
down_revision: str | Sequence[str] | None = "d5e8b3a4f901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "actions",
        sa.Column("policy_revision", sa.String(length=64), server_default="dev-1", nullable=False),
    )
    op.add_column(
        "actions",
        sa.Column("scope_fingerprint", sa.String(length=64), server_default="", nullable=False),
    )
    op.add_column("actions", sa.Column("approval_request_id", sa.String(length=64)))
    op.add_column("actions", sa.Column("downstream_idempotency_key", sa.String(length=256)))
    op.add_column("actions", sa.Column("upstream_handle", sa.String(length=256)))
    op.add_column("actions", sa.Column("cancel_receipt", sa.JSON()))

    op.add_column(
        "action_attempts",
        sa.Column("status", sa.String(length=32), server_default="started", nullable=False),
    )
    op.add_column(
        "action_attempts",
        sa.Column("lease_epoch", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("action_attempts", sa.Column("downstream_idempotency_key", sa.String(length=256)))
    op.add_column(
        "action_attempts",
        sa.Column(
            "dispatch_evidence", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False
        ),
    )
    op.add_column("action_attempts", sa.Column("receipt", sa.JSON()))
    op.add_column("action_attempts", sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        op.f("ck_action_attempts_status_valid"),
        "action_attempts",
        "status IN ('started', 'succeeded', 'failed', 'outcome_unknown')",
    )

    op.create_table(
        "input_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64)),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_schema", sa.JSON(), nullable=False),
        sa.Column("target_summary", sa.Text()),
        sa.Column("capability_revision", sa.String(length=128)),
        sa.Column("policy_revision", sa.String(length=64), nullable=False),
        sa.Column("scope_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("resource_versions", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('approval', 'clarification', 'external_input')",
            name=op.f("ck_input_requests_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'answered', 'denied', 'expired')",
            name=op.f("ck_input_requests_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_input_requests_action_id_actions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_input_requests_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_input_requests")),
    )
    op.create_index(op.f("ix_input_requests_run_id"), "input_requests", ["run_id"])
    op.create_index(op.f("ix_input_requests_action_id"), "input_requests", ["action_id"])
    op.create_index(
        "ix_input_requests_owner_status",
        "input_requests",
        ["tenant_id", "subject_id", "status"],
    )

    op.create_table(
        "input_responses",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("input_request_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_version", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("values", sa.JSON()),
        sa.Column("comment", sa.Text()),
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'deny', 'submit')",
            name=op.f("ck_input_responses_decision_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["input_request_id"],
            ["input_requests.id"],
            name=op.f("fk_input_responses_input_request_id_input_requests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_input_responses")),
        sa.UniqueConstraint("input_request_id", name="input_response_once"),
        sa.UniqueConstraint(
            "tenant_id", "subject_id", "idempotency_key", name="input_response_idempotency"
        ),
    )
    op.create_index(
        op.f("ix_input_responses_input_request_id"),
        "input_responses",
        ["input_request_id"],
    )

    op.create_table(
        "reconciliations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("action_version", sa.Integer(), nullable=False),
        sa.Column("resolution", sa.String(length=32), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("comment", sa.Text()),
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "resolution IN ('confirmed_applied', 'confirmed_not_applied', 'confirmed_stopped')",
            name=op.f("ck_reconciliations_resolution_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_reconciliations_action_id_actions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_reconciliations_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliations")),
        sa.UniqueConstraint("action_id", "idempotency_key", name="reconciliation_idempotency"),
    )
    op.create_index(op.f("ix_reconciliations_action_id"), "reconciliations", ["action_id"])
    op.create_index(op.f("ix_reconciliations_run_id"), "reconciliations", ["run_id"])

    op.create_table(
        "checkpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("checkpoint_seq", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("capability_id", sa.String(length=128)),
        sa.Column("capability_revision", sa.String(length=128)),
        sa.Column("policy_revision", sa.String(length=64), nullable=False),
        sa.Column("runner_cursor", sa.JSON(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_checkpoints_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checkpoints")),
        sa.UniqueConstraint("run_id", "checkpoint_seq", name="run_checkpoint"),
    )
    op.create_index(op.f("ix_checkpoints_run_id"), "checkpoints", ["run_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_checkpoints_run_id"), table_name="checkpoints")
    op.drop_table("checkpoints")
    op.drop_index(op.f("ix_reconciliations_run_id"), table_name="reconciliations")
    op.drop_index(op.f("ix_reconciliations_action_id"), table_name="reconciliations")
    op.drop_table("reconciliations")
    op.drop_index(op.f("ix_input_responses_input_request_id"), table_name="input_responses")
    op.drop_table("input_responses")
    op.drop_index("ix_input_requests_owner_status", table_name="input_requests")
    op.drop_index(op.f("ix_input_requests_action_id"), table_name="input_requests")
    op.drop_index(op.f("ix_input_requests_run_id"), table_name="input_requests")
    op.drop_table("input_requests")
    op.drop_constraint(op.f("ck_action_attempts_status_valid"), "action_attempts", type_="check")
    op.drop_column("action_attempts", "completed_at")
    op.drop_column("action_attempts", "receipt")
    op.drop_column("action_attempts", "dispatch_evidence")
    op.drop_column("action_attempts", "downstream_idempotency_key")
    op.drop_column("action_attempts", "lease_epoch")
    op.drop_column("action_attempts", "status")
    op.drop_column("actions", "cancel_receipt")
    op.drop_column("actions", "upstream_handle")
    op.drop_column("actions", "downstream_idempotency_key")
    op.drop_column("actions", "approval_request_id")
    op.drop_column("actions", "scope_fingerprint")
    op.drop_column("actions", "policy_revision")
