"""Tool policy gateway: role-tool bindings, idempotent call records, HITL approvals.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "app_runtime"
NEW_TABLES = ("role_tool_bindings", "tool_calls", "tool_approvals")


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _tenant_fk() -> sa.Column:
    return sa.Column(
        "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "role_tool_bindings",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("agent_role", sa.Text, nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("requires_approval", sa.Boolean, nullable=False, server_default=sa.false()),
        _now("created_at"),
        sa.UniqueConstraint("tenant_id", "agent_role", "tool_name", name="uq_role_tool_bindings"),
    )

    op.create_table(
        "tool_calls",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("agent_role", sa.Text, nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("arguments", postgresql.JSONB, nullable=False),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(
            "status IN ('running','success','pending_approval','error','rejected')", name="ck_tool_calls_status"
        ),
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_tool_calls_tenant_idempotency_key
        ON tool_calls (tenant_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )

    op.create_table(
        "tool_approvals",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "tool_call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tool_calls.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("resolver", sa.Text, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        _now("requested_at"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending','approved','rejected')", name="ck_tool_approvals_status"),
    )
    op.create_index("ix_tool_approvals_tenant_status", "tool_approvals", ["tenant_id", "status"])

    for table in NEW_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
            """
        )

    op.add_column("tool_call_logs", sa.Column("session_id", sa.Text, nullable=True))
    op.add_column("tool_call_logs", sa.Column("agent_role", sa.Text, nullable=True))
    op.add_column("tool_call_logs", sa.Column("call_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("tool_call_logs", sa.Column("error_code", sa.Text, nullable=True))
    op.drop_constraint("ck_tool_call_logs_status", "tool_call_logs", type_="check")
    op.execute("UPDATE tool_call_logs SET status = 'success' WHERE status = 'ok'")
    op.create_check_constraint(
        "ck_tool_call_logs_status",
        "tool_call_logs",
        "status IN ('success','pending_approval','error','rejected','rate_limited')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tool_call_logs_status", "tool_call_logs", type_="check")
    op.execute("UPDATE tool_call_logs SET status = 'ok' WHERE status IN ('success', 'pending_approval')")
    op.execute("UPDATE tool_call_logs SET status = 'rejected' WHERE status = 'rate_limited'")
    op.create_check_constraint(
        "ck_tool_call_logs_status", "tool_call_logs", "status IN ('ok','error','rejected')"
    )
    op.drop_column("tool_call_logs", "error_code")
    op.drop_column("tool_call_logs", "call_id")
    op.drop_column("tool_call_logs", "agent_role")
    op.drop_column("tool_call_logs", "session_id")

    for table in reversed(NEW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("tool_approvals")
    op.execute("DROP INDEX IF EXISTS uq_tool_calls_tenant_idempotency_key")
    op.drop_table("tool_calls")
    op.drop_table("role_tool_bindings")
