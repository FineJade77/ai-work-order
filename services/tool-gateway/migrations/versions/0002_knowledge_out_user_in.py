"""Knowledge moves to knowledge-service; tool calls record which employee they were made for.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = os.environ.get("DB_APP_ROLE", "tool_gateway_app")


def upgrade() -> None:
    op.drop_table("knowledge_items")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    # Nullable: rows written before this revision have no user.
    op.add_column("tool_calls", sa.Column("user_id", UUID(as_uuid=True), nullable=True))
    op.add_column("tool_call_logs", sa.Column("user_id", UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tool_call_logs", "user_id")
    op.drop_column("tool_calls", "user_id")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "knowledge_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("tags", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("CREATE INDEX ix_knowledge_items_question_trgm ON knowledge_items USING gin (question gin_trgm_ops)")
    op.execute("CREATE INDEX ix_knowledge_items_answer_trgm ON knowledge_items USING gin (answer gin_trgm_ops)")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_items TO {APP_ROLE}")
    op.execute("ALTER TABLE knowledge_items ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE knowledge_items FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON knowledge_items
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )
