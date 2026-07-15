"""Create durable dispatch intents.

Revision ID: 0003_dispatch_intents
Revises: 0002_operations_and_events
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_dispatch_intents"
down_revision: str | Sequence[str] | None = "0002_operations_and_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dispatch_intents",
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["operation_id"], ["operations.operation_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("operation_id"),
    )


def downgrade() -> None:
    op.drop_table("dispatch_intents")
