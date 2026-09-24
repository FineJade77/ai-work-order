"""Login (tenant slug, password hash, roles) and turns: one row per user message sent to agent-runtime.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-24

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = os.environ.get("DB_APP_ROLE", "app_service_app")


def upgrade() -> None:
    # tenants has no RLS, so login can resolve the slug before it knows the tenant.
    op.add_column("tenants", sa.Column("slug", sa.Text, nullable=True))
    op.execute("UPDATE tenants SET slug = 'tenant-' || left(id::text, 8) WHERE slug IS NULL")
    op.alter_column("tenants", "slug", nullable=False)
    op.create_unique_constraint("uq_tenants_slug", "tenants", ["slug"])

    # NULL: the account cannot log in (e.g. provisioned but not yet activated).
    op.add_column("users", sa.Column("password_hash", sa.Text, nullable=True))
    op.create_check_constraint("ck_users_role", "users", "role IN ('employee','it_staff')")

    op.create_table(
        "turns",
        # Also the task_id agent-runtime sees; its idempotency keys derive from it.
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_message_id", UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("outcome", sa.Text, nullable=True),
        sa.Column("ticket_id", UUID(as_uuid=True), nullable=True),  # ticket-service's id; no cross-database FK
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("enqueue_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued','done','failed')", name="ck_turns_status"),
    )
    # At most one turn in flight per conversation: a second message while one is queued is refused.
    op.execute("CREATE UNIQUE INDEX uq_turns_one_queued_per_conversation ON turns (conversation_id) WHERE status = 'queued'")
    op.execute("CREATE INDEX ix_turns_queued_updated ON turns (updated_at) WHERE status = 'queued'")

    op.add_column("messages", sa.Column("task_id", UUID(as_uuid=True), nullable=True))
    op.create_check_constraint("ck_messages_role", "messages", "role IN ('user','assistant')")
    # The assistant's reply to a turn is stored once, however often its result is delivered.
    op.execute("CREATE UNIQUE INDEX uq_messages_task_role ON messages (task_id, role) WHERE task_id IS NOT NULL")
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE ON turns TO {APP_ROLE}")
    op.execute("ALTER TABLE turns ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE turns FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON turns
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.execute("DROP INDEX uq_messages_task_role")
    op.drop_constraint("ck_messages_role", "messages")
    op.drop_column("messages", "task_id")
    op.drop_table("turns")
    op.drop_constraint("ck_users_role", "users")
    op.drop_column("users", "password_hash")
    op.drop_constraint("uq_tenants_slug", "tenants")
    op.drop_column("tenants", "slug")
