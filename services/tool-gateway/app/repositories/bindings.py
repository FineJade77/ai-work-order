from sqlalchemy import select

from app.db.models import RoleToolBinding
from app.db.session import session_factory, tenant_scope


async def tool_bindings(tenant_id: str, agent_role: str) -> dict[str, bool]:
    """The tools this role may call in this tenant, each with whether it needs a handler's approval."""
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        stmt = select(RoleToolBinding.tool_name, RoleToolBinding.requires_approval).where(
            RoleToolBinding.agent_role == agent_role
        )
        return {name: requires_approval for name, requires_approval in (await session.execute(stmt)).all()}
