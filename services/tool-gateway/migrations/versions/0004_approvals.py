"""Human approval of side-effecting tool calls (HITL), and the mock access-grant store.

- role_tool_bindings.requires_approval: per tenant and role, whether a tool waits for a handler.
- tool_calls.status gains 'pending_approval'.
- tool_approvals: one per call that needs approval; tenant data, so RLS as usual.
- approval_outbox: work for the background relay (publish a decision, check an expiry). It holds
  routing ids only, no content, and has no RLS: the relay serves every tenant, and switches into a
  tenant's scope before it reads that tenant's approval.
- access_grants: what MockAccessProvider "grants" (the grant_access tool), deduplicated by the
  call's idempotency key like any downstream system must be.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = os.environ.get("DB_APP_ROLE", "tool_gateway_app")


def _pk() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _ts(name: str, nullable: bool = False) -> sa.Column:
    default = None if nullable else sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=default)


def _tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def upgrade() -> None:
    op.add_column(
        "role_tool_bindings", sa.Column("requires_approval", sa.Boolean, nullable=False, server_default=sa.false())
    )

    op.drop_constraint("ck_tool_calls_status", "tool_calls")
    op.create_check_constraint(
        "ck_tool_calls_status", "tool_calls", "status IN ('running','success','error','pending_approval')"
    )

    # The audit row of a call that is now waiting for a handler.
    op.drop_constraint("ck_tool_call_logs_status", "tool_call_logs")
    op.create_check_constraint(
        "ck_tool_call_logs_status", "tool_call_logs", "status IN ('success','error','rejected','pending_approval')"
    )

    op.create_table(
        "tool_approvals",
        _pk(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("call_id", UUID(as_uuid=True), sa.ForeignKey("tool_calls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),  # the conversation
        sa.Column("requester_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_role", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        # The trace the approval was requested in; the resumed run links back to it.
        sa.Column("traceparent", sa.Text, nullable=True),
        _ts("expires_at"),
        sa.Column("decided_by", UUID(as_uuid=True), nullable=True),
        _ts("decided_at", nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("call_id", name="uq_tool_approvals_call"),
        sa.CheckConstraint("status IN ('pending','approved','rejected','expired')", name="ck_tool_approvals_status"),
    )
    op.create_index("ix_tool_approvals_tenant_status_created", "tool_approvals", ["tenant_id", "status", "created_at"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON tool_approvals TO {APP_ROLE}")
    _tenant_rls("tool_approvals")

    op.create_table(
        "approval_outbox",
        _pk(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        _ts("due_at"),
        _ts("done_at", nullable=True),
        _ts("created_at"),
        sa.CheckConstraint("kind IN ('publish','expire')", name="ck_approval_outbox_kind"),
    )
    op.execute("CREATE INDEX ix_approval_outbox_due ON approval_outbox (due_at) WHERE done_at IS NULL")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON approval_outbox TO {APP_ROLE}")

    op.create_table(
        "access_grants",
        _pk(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("system", sa.Text, nullable=False),
        sa.Column("access_level", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_access_grants_key"),
    )
    op.execute(f"GRANT SELECT, INSERT ON access_grants TO {APP_ROLE}")
    _tenant_rls("access_grants")


def downgrade() -> None:
    op.drop_table("access_grants")
    op.drop_table("approval_outbox")
    op.drop_table("tool_approvals")
    # FORCE ROW LEVEL SECURITY binds the owner too; lifted so the delete sees every tenant's rows.
    op.execute("ALTER TABLE tool_calls NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM tool_calls WHERE status = 'pending_approval'")
    op.execute("ALTER TABLE tool_calls FORCE ROW LEVEL SECURITY")
    op.drop_constraint("ck_tool_calls_status", "tool_calls")
    op.create_check_constraint("ck_tool_calls_status", "tool_calls", "status IN ('running','success','error')")
    op.drop_column("role_tool_bindings", "requires_approval")
    op.execute("ALTER TABLE tool_call_logs NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE tool_call_logs SET status = 'error', error_code = 'PENDING_APPROVAL' WHERE status = 'pending_approval'"
    )
    op.execute("ALTER TABLE tool_call_logs FORCE ROW LEVEL SECURITY")
    op.drop_constraint("ck_tool_call_logs_status", "tool_call_logs")
    op.create_check_constraint("ck_tool_call_logs_status", "tool_call_logs", "status IN ('success','error','rejected')")
