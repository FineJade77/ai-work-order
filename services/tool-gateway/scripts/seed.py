"""Local demo data. Run with the owner account:
docker compose run --rm tool-gateway-migrate python scripts/seed.py

Knowledge content lives in knowledge-service; this only binds tools to the demo agent role.
"""

import asyncio
import os

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.models import RoleToolBinding
from app.db.session import tenant_scope

# Fixed IDs shared with the other services' demo data; no cross-database FK enforces them.
DEMO_TENANTS = ["e0e112f3-5afb-4229-869b-c0bd012324bb", "9b2d6c1a-4e7f-4a3b-8c5d-2f1e0a9b8c7d"]
AGENT_ROLE = "it_support_agent"
ROLE_TOOLS = ["search_knowledge", "create_ticket"]


async def main() -> None:
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    for tenant_id in DEMO_TENANTS:
        # The owner account is subject to FORCE ROW LEVEL SECURITY too, so seed inside a tenant scope.
        async with AsyncSession(engine) as session, tenant_scope(session, tenant_id):
            for tool_name in ROLE_TOOLS:
                await session.execute(
                    pg_insert(RoleToolBinding)
                    .values(tenant_id=tenant_id, agent_role=AGENT_ROLE, tool_name=tool_name)
                    .on_conflict_do_nothing(index_elements=["tenant_id", "agent_role", "tool_name"])
                )
            await session.commit()
        print(f"tenant_id={tenant_id} role={AGENT_ROLE} tools={ROLE_TOOLS}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
