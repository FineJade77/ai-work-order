"""Add tool_call_logs audit table and pg_trgm indexes for knowledge search ranking.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "app_runtime"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_items_question_trgm "
        "ON knowledge_items USING gin (question gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_items_answer_trgm "
        "ON knowledge_items USING gin (answer gin_trgm_ops)"
    )

    op.create_table(
        "tool_call_logs",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("params_summary", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('ok','error','rejected')", name="ck_tool_call_logs_status"),
    )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tool_call_logs TO {APP_ROLE}")
    op.execute("ALTER TABLE tool_call_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tool_call_logs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON tool_call_logs
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tool_call_logs")
    op.drop_table("tool_call_logs")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_items_answer_trgm")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_items_question_trgm")
