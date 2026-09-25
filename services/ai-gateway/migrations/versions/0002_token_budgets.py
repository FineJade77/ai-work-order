"""Daily token budgets: per-tenant overrides, and an index to sum a tenant's usage for today.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25

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

APP_ROLE = os.environ.get("DB_APP_ROLE", "ai_gateway_app")


def upgrade() -> None:
    # Every /v1/generate sums the tenant's usage since the start of its budget day.
    op.create_index("ix_model_call_logs_tenant_created", "model_call_logs", ["tenant_id", "created_at"])

    op.create_table(
        "tenant_token_budgets",
        sa.Column("tenant_id", UUID(as_uuid=True), primary_key=True),  # no FK: tenants live in app-service
        sa.Column("daily_tokens", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("daily_tokens >= 0", name="ck_tenant_token_budgets_nonnegative"),
    )
    # Read-only at runtime; budgets are set with SQL as the owner. ENABLE without FORCE: the
    # runtime account is not the owner, so the policy binds it, while the owner can set any
    # tenant's budget without first putting a tenant in scope.
    op.execute(f"GRANT SELECT ON tenant_token_budgets TO {APP_ROLE}")
    op.execute("ALTER TABLE tenant_token_budgets ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON tenant_token_budgets
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.drop_table("tenant_token_budgets")
    op.drop_index("ix_model_call_logs_tenant_created", table_name="model_call_logs")
