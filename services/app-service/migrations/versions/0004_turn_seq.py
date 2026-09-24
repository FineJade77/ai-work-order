"""turns.seq: a turn's position in its conversation, sent with its task so agent-runtime can
recognise and skip a stale copy of an older turn.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("seq", sa.Integer, nullable=True))
    # FORCE ROW LEVEL SECURITY binds the owner too, and there is no tenant in scope here, so the
    # backfill would see no rows. Lifted for the backfill only; the migration is one transaction.
    op.execute("ALTER TABLE turns NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        UPDATE turns t SET seq = n.seq
        FROM (SELECT id, row_number() OVER (PARTITION BY conversation_id ORDER BY created_at, id) AS seq FROM turns) n
        WHERE t.id = n.id
        """
    )
    op.execute("ALTER TABLE turns FORCE ROW LEVEL SECURITY")
    op.alter_column("turns", "seq", nullable=False)
    op.create_check_constraint("ck_turns_seq_positive", "turns", "seq >= 1")
    op.create_unique_constraint("uq_turns_conversation_seq", "turns", ["conversation_id", "seq"])


def downgrade() -> None:
    op.drop_constraint("uq_turns_conversation_seq", "turns")
    op.drop_constraint("ck_turns_seq_positive", "turns")
    op.drop_column("turns", "seq")
