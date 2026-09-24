"""app-service schema: tenants, users, conversations, messages, tickets.

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

APP_ROLE = os.environ.get("DB_APP_ROLE", "app_service_app")
TABLES = ("tenants", "users", "conversations", "messages", "tickets")
TENANT_COLUMN_TABLES = ("users", "conversations", "tickets")


def _pk() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _tenant_fk() -> sa.Column:
    return sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "tenants",
        _pk(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("plan", sa.Text, nullable=False, server_default="standard"),
        _created_at(),
    )
    op.create_table(
        "users",
        _pk(),
        _tenant_fk(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False, server_default="employee"),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )
    op.create_table(
        "conversations",
        _pk(),
        _tenant_fk(),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        _created_at(),
    )
    op.create_table(
        "messages",
        _pk(),
        sa.Column(
            "conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        _created_at(),
    )
    op.create_table(
        "tickets",
        _pk(),
        _tenant_fk(),
        sa.Column(
            "conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("assignee", sa.Text, nullable=True),
        sa.Column("idempotency_key", sa.Text, nullable=True),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('draft','open','in_progress','resolved','closed','reopened')", name="ck_tickets_status"
        ),
    )
    # Partial: only tickets created through the retry-prone tool path carry a key.
    op.execute(
        "CREATE UNIQUE INDEX uq_tickets_tenant_idempotency_key "
        "ON tickets (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
    )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {APP_ROLE}")

    for table in TENANT_COLUMN_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
            """
        )
    # messages has no tenant_id of its own; its policy goes through the parent conversation.
    op.execute("ALTER TABLE messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE messages FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON messages
        USING (EXISTS (SELECT 1 FROM conversations c WHERE c.id = messages.conversation_id
                       AND c.tenant_id = current_setting('app.tenant_id', true)::uuid))
        WITH CHECK (EXISTS (SELECT 1 FROM conversations c WHERE c.id = messages.conversation_id
                            AND c.tenant_id = current_setting('app.tenant_id', true)::uuid))
        """
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
