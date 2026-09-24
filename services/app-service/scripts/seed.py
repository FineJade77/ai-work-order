"""Local demo data. Run with the owner account:
docker compose run --rm app-service-migrate python scripts/seed.py
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Conversation, Tenant, User
from app.db.session import tenant_scope

# Fixed so other services' seeds can refer to the same tenant without cross-database FKs.
DEMO_TENANT_ID = "e0e112f3-5afb-4229-869b-c0bd012324bb"
DEMO_USER_ID = "7c1f3a2e-9b4d-4e6a-8f10-2d5c6b7a8e91"
DEMO_CONVERSATION_ID = "3e653ec1-1f5d-407b-aad9-b751ec89c589"


def main() -> None:
    engine = create_engine(os.environ["MIGRATION_DATABASE_URL"])
    with Session(engine) as session, tenant_scope(session, DEMO_TENANT_ID):
        session.execute(
            pg_insert(Tenant).values(id=DEMO_TENANT_ID, name="Demo Tenant").on_conflict_do_nothing()
        )
        session.execute(
            pg_insert(User)
            .values(id=DEMO_USER_ID, tenant_id=DEMO_TENANT_ID, name="Demo User", email="demo.user@example.com")
            .on_conflict_do_nothing()
        )
        session.execute(
            pg_insert(Conversation)
            .values(id=DEMO_CONVERSATION_ID, tenant_id=DEMO_TENANT_ID, user_id=DEMO_USER_ID)
            .on_conflict_do_nothing()
        )
        session.commit()
    print(f"tenant_id={DEMO_TENANT_ID} conversation_id={DEMO_CONVERSATION_ID}")


if __name__ == "__main__":
    main()
