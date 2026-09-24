"""Local demo data. Run with the owner account:
docker compose run --rm app-service-migrate python scripts/seed.py

Every demo account's password is SEED_PASSWORD (default "demo-password"). Log in with
POST /v1/auth/login {"tenant": "demo", "email": "demo.user@example.com", "password": "demo-password"}.
"""

import asyncio
import os

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.security import hash_password
from app.db.models import Conversation, Tenant, User
from app.db.session import tenant_scope

PASSWORD = os.environ.get("SEED_PASSWORD", "demo-password")

# Fixed IDs shared with the other services' demo data (knowledge-service's static data,
# tool-gateway's role bindings); no cross-database FK enforces them.
DEMO_TENANTS = [
    {
        "tenant_id": "e0e112f3-5afb-4229-869b-c0bd012324bb",
        "slug": "demo",
        "name": "Demo Tenant",
        "users": [
            ("7c1f3a2e-9b4d-4e6a-8f10-2d5c6b7a8e91", "Demo User", "demo.user@example.com", "employee"),
            ("2b6f0c1d-8e3a-4f7b-9c2d-5a1e3f4b6c7d", "IT Staff", "it.staff@example.com", "it_staff"),
        ],
        "conversation_id": "3e653ec1-1f5d-407b-aad9-b751ec89c589",
    },
    {
        "tenant_id": "9b2d6c1a-4e7f-4a3b-8c5d-2f1e0a9b8c7d",
        "slug": "northwind",
        "name": "Northwind 分公司",
        "users": [
            ("4d8e2b61-3c7a-4f95-a0b1-6e5d4c3b2a19", "Demo User", "demo.user@northwind.example", "employee"),
            ("8f3e1a2b-6c4d-4e5f-a7b8-c9d0e1f2a3b4", "IT Staff", "it.staff@northwind.example", "it_staff"),
        ],
        "conversation_id": "c5a9e7d3-1b2f-4c6e-8a0d-9f8e7d6c5b4a",
    },
]


async def main() -> None:
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    for t in DEMO_TENANTS:
        async with AsyncSession(engine) as session, tenant_scope(session, t["tenant_id"]):
            tenant = pg_insert(Tenant).values(id=t["tenant_id"], name=t["name"], slug=t["slug"])
            await session.execute(tenant.on_conflict_do_update(index_elements=["id"], set_={"slug": t["slug"]}))
            for user_id, name, email, role in t["users"]:
                user = pg_insert(User).values(
                    id=user_id, tenant_id=t["tenant_id"], name=name, email=email, role=role,
                    password_hash=hash_password(PASSWORD),
                )
                await session.execute(
                    user.on_conflict_do_update(
                        index_elements=["id"], set_={"role": role, "password_hash": user.excluded.password_hash}
                    )
                )
            employee_id = t["users"][0][0]
            await session.execute(
                pg_insert(Conversation)
                .values(id=t["conversation_id"], tenant_id=t["tenant_id"], user_id=employee_id)
                .on_conflict_do_nothing()
            )
            await session.commit()
        accounts = ", ".join(f"{email} ({role})" for _, _, email, role in t["users"])
        print(f"tenant={t['slug']} tenant_id={t['tenant_id']} accounts: {accounts}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
