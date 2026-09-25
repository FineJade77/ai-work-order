"""The staff role is no longer IT-specific: it_staff becomes handler.

Whoever handles a tenant's tickets (IT, HR, admin...) has this role. Tokens issued before the
rename carry it_staff and are refused as invalid; users log in again.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rename(old: str, new: str) -> None:
    op.drop_constraint("ck_users_role", "users")
    # FORCE ROW LEVEL SECURITY binds the owner too, and no tenant is in scope here, so the update
    # would see no rows. Lifted for the update only; the migration is one transaction.
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    op.execute(f"UPDATE users SET role = '{new}' WHERE role = '{old}'")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.create_check_constraint("ck_users_role", "users", f"role IN ('employee','{new}')")


def upgrade() -> None:
    _rename("it_staff", "handler")


def downgrade() -> None:
    _rename("handler", "it_staff")
