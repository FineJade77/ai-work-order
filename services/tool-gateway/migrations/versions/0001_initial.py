"""tool-gateway schema: knowledge_items, role_tool_bindings, tool_calls, tool_call_logs.

Revision ID: 0001
Revises:
Create Date: 2026-09-24

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = os.environ.get("DB_APP_ROLE", "tool_gateway_app")
TABLES = ("knowledge_items", "role_tool_bindings", "tool_calls", "tool_call_logs")


def _pk() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _tenant_id() -> sa.Column:
    # No FK: tenants live in app-service's database.
    return sa.Column("tenant_id", UUID(as_uuid=True), nullable=False)


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "knowledge_items",
        _pk(),
        _tenant_id(),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("tags", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source", sa.Text, nullable=True),
        _ts("updated_at"),
    )
    op.execute("CREATE INDEX ix_knowledge_items_question_trgm ON knowledge_items USING gin (question gin_trgm_ops)")
    op.execute("CREATE INDEX ix_knowledge_items_answer_trgm ON knowledge_items USING gin (answer gin_trgm_ops)")

    op.create_table(
        "role_tool_bindings",
        _pk(),
        _tenant_id(),
        sa.Column("agent_role", sa.Text, nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("tenant_id", "agent_role", "tool_name", name="uq_role_tool_bindings"),
    )

    op.create_table(
        "tool_calls",
        _pk(),
        _tenant_id(),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("agent_role", sa.Text, nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("status IN ('running','success','error')", name="ck_tool_calls_status"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_tool_calls_tenant_idempotency_key "
        "ON tool_calls (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
    )

    op.create_table(
        "tool_call_logs",
        _pk(),
        _tenant_id(),
        sa.Column("session_id", sa.Text, nullable=True),
        sa.Column("agent_role", sa.Text, nullable=True),
        sa.Column("call_id", UUID(as_uuid=True), nullable=True),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("params_summary", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        _ts("created_at"),
        sa.CheckConstraint("status IN ('success','error','rejected')", name="ck_tool_call_logs_status"),
    )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {APP_ROLE}")
    for table in TABLES:
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
    for table in reversed(TABLES):
        op.drop_table(table)
