from app.db.models import RoleToolBinding
from app.db.session import session_factory, tenant_scope


def bound_tool_names(tenant_id: str, agent_role: str) -> set[str]:
    Session = session_factory()
    with Session() as session, tenant_scope(session, tenant_id):
        rows = session.query(RoleToolBinding.tool_name).filter(RoleToolBinding.agent_role == agent_role).all()
    return {name for (name,) in rows}
