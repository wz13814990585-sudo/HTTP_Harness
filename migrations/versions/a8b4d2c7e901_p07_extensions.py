"""Pin skills and persist bounded child-run lineage.

Revision ID: a8b4d2c7e901
Revises: f7a0d5c6b123
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8b4d2c7e901"
down_revision: str | Sequence[str] | None = "f7a0d5c6b123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("skill_refs", sa.JSON(), nullable=True))
    op.add_column("runs", sa.Column("parent_run_id", sa.String(length=64), nullable=True))
    op.add_column(
        "runs",
        sa.Column("child_depth", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("runs", sa.Column("delegated_scopes", sa.JSON(), nullable=True))
    op.create_foreign_key(
        op.f("fk_runs_parent_run_id_runs"),
        "runs",
        "runs",
        ["parent_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_runs_parent_run_id"), "runs", ["parent_run_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_runs_parent_run_id"), table_name="runs")
    op.drop_constraint(op.f("fk_runs_parent_run_id_runs"), "runs", type_="foreignkey")
    op.drop_column("runs", "delegated_scopes")
    op.drop_column("runs", "child_depth")
    op.drop_column("runs", "parent_run_id")
    op.drop_column("runs", "skill_refs")
