"""Create operations and operation events.

Revision ID: 0002_operations_and_events
Revises: 0001_bootstrap
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_operations_and_events"
down_revision: str | Sequence[str] | None = "0001_bootstrap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operations",
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider_payment_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_operations_positive_amount"),
        sa.CheckConstraint("currency = 'RUB'", name="ck_operations_rub_currency"),
        sa.CheckConstraint(
            "status IN ('CREATED', 'PROCESSING', 'COMPLETED', 'REJECTED')",
            name="ck_operations_status",
        ),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint("provider_payment_id", name="uq_operations_provider_payment_id"),
    )
    op.create_table(
        "operation_events",
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["operation_id"], ["operations.operation_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("operation_id", "event_id"),
    )


def downgrade() -> None:
    op.drop_table("operation_events")
    op.drop_table("operations")
