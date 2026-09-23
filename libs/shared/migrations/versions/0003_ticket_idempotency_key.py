"""Add tickets.idempotency_key so create_ticket retries don't create duplicate tickets.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("idempotency_key", sa.Text, nullable=True))
    # Partial (not plain) unique index: idempotency_key is only required for tickets
    # created through the create_ticket tool's crash-and-retry path (see
    # implementation-plan.md phase 2). Other creation paths may leave it null, and
    # nulls must not collide with each other.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_tickets_tenant_idempotency_key
        ON tickets (tenant_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_tickets_tenant_idempotency_key")
    op.drop_column("tickets", "idempotency_key")
