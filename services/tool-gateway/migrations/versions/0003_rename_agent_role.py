"""The agent role is no longer IT-specific: it_support_agent becomes service_agent.

Only the bindings change. tool_calls and tool_call_logs keep the role a call was made under.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rename(old: str, new: str) -> None:
    # FORCE ROW LEVEL SECURITY binds the owner too, and no tenant is in scope here, so the rename
    # would see no rows. Lifted for the rename only; the migration is one transaction.
    op.execute("ALTER TABLE role_tool_bindings NO FORCE ROW LEVEL SECURITY")
    # A binding may already exist under the new name (e.g. seeded); keep that one, drop the old.
    op.execute(
        f"""
        UPDATE role_tool_bindings b SET agent_role = '{new}'
        WHERE agent_role = '{old}' AND NOT EXISTS (
            SELECT 1 FROM role_tool_bindings n
            WHERE n.tenant_id = b.tenant_id AND n.tool_name = b.tool_name AND n.agent_role = '{new}'
        )
        """
    )
    op.execute(f"DELETE FROM role_tool_bindings WHERE agent_role = '{old}'")
    op.execute("ALTER TABLE role_tool_bindings FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _rename("it_support_agent", "service_agent")


def downgrade() -> None:
    _rename("service_agent", "it_support_agent")
