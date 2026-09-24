"""Local demo data. Run with the owner account:
docker compose run --rm app-service-migrate python scripts/seed.py
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Conversation, Tenant, User
from app.db.session import tenant_scope

# Fixed IDs shared with the other services' demo data (knowledge-service's static data,
# tool-gateway's role bindings); no cross-database FK enforces them.
DEMO_TENANTS = [
    {
        "tenant_id": "e0e112f3-5afb-4229-869b-c0bd012324bb",
        "name": "Demo Tenant",
        "user_id": "7c1f3a2e-9b4d-4e6a-8f10-2d5c6b7a8e91",
        "email": "demo.user@example.com",
        "conversation_id": "3e653ec1-1f5d-407b-aad9-b751ec89c589",
    },
    {
        "tenant_id": "9b2d6c1a-4e7f-4a3b-8c5d-2f1e0a9b8c7d",
        "name": "Northwind 分公司",
        "user_id": "4d8e2b61-3c7a-4f95-a0b1-6e5d4c3b2a19",
        "email": "demo.user@northwind.example",
        "conversation_id": "c5a9e7d3-1b2f-4c6e-8a0d-9f8e7d6c5b4a",
    },
]


def main() -> None:
    engine = create_engine(os.environ["MIGRATION_DATABASE_URL"])
    for t in DEMO_TENANTS:
        with Session(engine) as session, tenant_scope(session, t["tenant_id"]):
            session.execute(pg_insert(Tenant).values(id=t["tenant_id"], name=t["name"]).on_conflict_do_nothing())
            session.execute(
                pg_insert(User)
                .values(id=t["user_id"], tenant_id=t["tenant_id"], name="Demo User", email=t["email"])
                .on_conflict_do_nothing()
            )
            session.execute(
                pg_insert(Conversation)
                .values(id=t["conversation_id"], tenant_id=t["tenant_id"], user_id=t["user_id"])
                .on_conflict_do_nothing()
            )
            session.commit()
        print(f"tenant_id={t['tenant_id']} user_id={t['user_id']} conversation_id={t['conversation_id']}")


if __name__ == "__main__":
    main()
