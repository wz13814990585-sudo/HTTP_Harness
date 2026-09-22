"""Persist the admission-time scope ceiling for resumable Run authorization.

Revision ID: b9c5e3d8f012
Revises: a8b4d2c7e901
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9c5e3d8f012"
down_revision: str | Sequence[str] | None = "a8b4d2c7e901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Legacy Runs get an empty ceiling and therefore cannot acquire tool
    # scopes merely because a future worker has broader service credentials.
    op.add_column(
        "runs",
        sa.Column("admitted_scopes", sa.JSON(), server_default="[]", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("runs", "admitted_scopes")
