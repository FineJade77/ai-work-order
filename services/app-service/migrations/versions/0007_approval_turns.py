"""Approval turns: a turn can carry a handler's decision instead of an employee's message.

When an approval the conversation is waiting for is decided, app-service opens a turn of kind
'approval' (no user message) whose task resumes the paused agent run. approval_id is unique, so
a decision event delivered twice opens one turn.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("kind", sa.Text, nullable=False, server_default="message"))
    op.create_check_constraint("ck_turns_kind", "turns", "kind IN ('message','approval')")
    op.alter_column("turns", "user_message_id", nullable=True)
    op.add_column("turns", sa.Column("approval_id", UUID(as_uuid=True), nullable=True))
    op.add_column("turns", sa.Column("approval_decision", sa.Text, nullable=True))
    op.add_column("turns", sa.Column("approval_note", sa.Text, nullable=True))
    op.create_unique_constraint("uq_turns_approval", "turns", ["approval_id"])
    op.create_check_constraint(
        "ck_turns_kind_fields",
        "turns",
        "(kind = 'message' AND user_message_id IS NOT NULL AND approval_id IS NULL)"
        " OR (kind = 'approval' AND approval_id IS NOT NULL AND approval_decision IS NOT NULL)",
    )


def downgrade() -> None:
    # FORCE ROW LEVEL SECURITY binds the owner too; lifted so the delete sees every tenant's rows.
    op.execute("ALTER TABLE turns NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM turns WHERE kind = 'approval'")
    op.execute("ALTER TABLE turns FORCE ROW LEVEL SECURITY")
    op.drop_constraint("ck_turns_kind_fields", "turns")
    op.drop_constraint("uq_turns_approval", "turns")
    op.drop_column("turns", "approval_note")
    op.drop_column("turns", "approval_decision")
    op.drop_column("turns", "approval_id")
    op.alter_column("turns", "user_message_id", nullable=False)
    op.drop_constraint("ck_turns_kind", "turns")
    op.drop_column("turns", "kind")
