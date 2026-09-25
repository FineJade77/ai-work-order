"""Local demo data. Run with the owner account:
docker compose run --rm tool-gateway-migrate python scripts/seed.py

Knowledge content lives in knowledge-service; this only binds tools to the demo agent role, and
sets the approval policy: grant_access waits for a handler in the demo tenant but runs straight
away in northwind, the same tool under two tenants' policies.
"""

import asyncio
import os

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.models import RoleToolBinding
from app.db.session import tenant_scope

# Fixed IDs shared with the other services' demo data; no cross-database FK enforces them.
DEMO_TENANTS = ["e0e112f3-5afb-4229-869b-c0bd012324bb", "9b2d6c1a-4e7f-4a3b-8c5d-2f1e0a9b8c7d"]
AGENT_ROLE = "service_agent"
# tool -> tenant ids whose policy makes it wait for approval
ROLE_TOOLS = {
    "search_knowledge": set(),
    "create_ticket": set(),
    "grant_access": {"e0e112f3-5afb-4229-869b-c0bd012324bb"},
}


async def main() -> None:
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    for tenant_id in DEMO_TENANTS:
        # The owner account is subject to FORCE ROW LEVEL SECURITY too, so seed inside a tenant scope.
        async with AsyncSession(engine) as session, tenant_scope(session, tenant_id):
            for tool_name, approval_tenants in ROLE_TOOLS.items():
                binding = pg_insert(RoleToolBinding).values(
                    tenant_id=tenant_id, agent_role=AGENT_ROLE, tool_name=tool_name,
                    requires_approval=tenant_id in approval_tenants,
                )
                await session.execute(
                    binding.on_conflict_do_update(
                        index_elements=["tenant_id", "agent_role", "tool_name"],
                        set_={"requires_approval": binding.excluded.requires_approval},
                    )
                )
            await session.commit()
        policy = {tool: ("approval" if tenant_id in tenants else "direct") for tool, tenants in ROLE_TOOLS.items()}
        print(f"tenant_id={tenant_id} role={AGENT_ROLE} tools={policy}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
