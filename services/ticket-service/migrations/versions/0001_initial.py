"""ticket-service schema: tickets, ticket_events.

Revision ID: 0001
Revises:
Create Date: 2026-09-24

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = os.environ.get("DB_APP_ROLE", "ticket_service_app")


def _pk() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "tickets",
        _pk(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("requester_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("assignee_id", UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("idempotency_key", sa.Text, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("status IN ('open','in_progress','resolved','closed')", name="ck_tickets_status"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_tickets_tenant_idempotency_key "
        "ON tickets (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    # Supports the list endpoint: newest first, filtered by requester or assignee.
    op.create_index("ix_tickets_tenant_requester_created", "tickets", ["tenant_id", "requester_id", "created_at"])
    op.create_index("ix_tickets_tenant_assignee_created", "tickets", ["tenant_id", "assignee_id", "created_at"])

    op.create_table(
        "ticket_events",
        _pk(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("from_status", sa.Text, nullable=True),
        sa.Column("to_status", sa.Text, nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_ticket_events_ticket_created", "ticket_events", ["ticket_id", "created_at"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE ON tickets TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON ticket_events TO {APP_ROLE}")  # history is append-only
    for table in ("tickets", "ticket_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
            """
        )


def downgrade() -> None:
    op.drop_table("ticket_events")
    op.drop_table("tickets")
