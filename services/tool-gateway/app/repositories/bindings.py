from sqlalchemy import select

from app.db.models import RoleToolBinding
from app.db.session import session_factory, tenant_scope


async def bound_tool_names(tenant_id: str, agent_role: str) -> set[str]:
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        stmt = select(RoleToolBinding.tool_name).where(RoleToolBinding.agent_role == agent_role)
        return set((await session.execute(stmt)).scalars())
