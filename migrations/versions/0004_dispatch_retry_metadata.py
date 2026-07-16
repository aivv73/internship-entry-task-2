"""Add durable dispatch retry scheduling.

Revision ID: 0004_dispatch_retry_metadata
Revises: 0003_dispatch_intents
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_dispatch_retry_metadata"
down_revision: str | Sequence[str] | None = "0003_dispatch_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dispatch_intents",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_dispatch_intents_due",
        "dispatch_intents",
        ["dispatched_at", "next_attempt_at", "claimed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dispatch_intents_due", table_name="dispatch_intents")
    op.drop_column("dispatch_intents", "next_attempt_at")
