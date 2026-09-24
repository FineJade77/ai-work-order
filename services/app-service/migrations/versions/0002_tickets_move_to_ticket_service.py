"""Tickets move to ticket-service, which owns ticket data and lifecycle.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = os.environ.get("DB_APP_ROLE", "app_service_app")


def upgrade() -> None:
    op.drop_table("tickets")


def downgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("assignee", sa.Text, nullable=True),
        sa.Column("idempotency_key", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('draft','open','in_progress','resolved','closed','reopened')", name="ck_tickets_status"
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_tickets_tenant_idempotency_key "
        "ON tickets (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tickets TO {APP_ROLE}")
    op.execute("ALTER TABLE tickets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tickets FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON tickets
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )
