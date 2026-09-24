"""Defer HITL approvals out of V1: drop tool_approvals and role_tool_bindings.requires_approval.

The tool_calls / tool_call_logs status CHECK constraints are deliberately left accepting
'pending_approval' and 'rate_limited': audit rows written before this revision carry those
values, and audit history is not rewritten.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "app_runtime"


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tool_approvals")
    op.drop_table("tool_approvals")
    op.drop_column("role_tool_bindings", "requires_approval")


def downgrade() -> None:
    op.add_column(
        "role_tool_bindings",
        sa.Column("requires_approval", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "tool_approvals",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
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
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending','approved','rejected')", name="ck_tool_approvals_status"),
    )
    op.create_index("ix_tool_approvals_tenant_status", "tool_approvals", ["tenant_id", "status"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tool_approvals TO {APP_ROLE}")
    op.execute("ALTER TABLE tool_approvals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tool_approvals FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON tool_approvals
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )
