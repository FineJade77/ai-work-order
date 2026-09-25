"""turns.traceparent: the W3C trace context of the request that created the turn.

The requeuer re-enqueues a turn under it, so a requeued task still lands in the turn's trace.
Null for turns created before this revision, or while tracing is off.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("traceparent", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("turns", "traceparent")
