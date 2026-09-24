from shared.db.models import RoleToolBinding
from shared.db.session import app_session_factory, tenant_scope

from app.registry import REGISTRY


def allowed_tools(tenant_id: str, agent_role: str) -> dict[str, bool]:
    """tool_name -> requires_approval for every registered tool this role may call in this tenant."""
    Session = app_session_factory()
    with Session() as session, tenant_scope(session, tenant_id):
        rows = (
            session.query(RoleToolBinding.tool_name, RoleToolBinding.requires_approval)
            .filter(RoleToolBinding.agent_role == agent_role)
            .all()
        )
    return {name: requires_approval for name, requires_approval in rows if name in REGISTRY}
