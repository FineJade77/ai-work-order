"""Creates the LangGraph checkpoint tables. Run with the owner account before the workers start:
docker compose run --rm agent-runtime-migrate

The schema belongs to langgraph-checkpoint-postgres (its setup() is versioned and re-runnable),
so it is not managed with Alembic. The tables are keyed by thread_id (= conversation_id) and
have no tenant_id column, so there is no RLS here; only agent-runtime can reach this database.
"""

import asyncio
import os

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql

APP_ROLE = os.environ.get("DB_APP_ROLE", "agent_runtime_app")
RUNTIME_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def main() -> None:
    async with AsyncPostgresSaver.from_conn_string(os.environ["MIGRATION_DATABASE_URL"]) as saver:
        await saver.setup()
        # Checkpoints are upserted, never deleted by the runtime.
        await saver.conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON {} TO {}").format(
                sql.SQL(", ").join(map(sql.Identifier, RUNTIME_TABLES)), sql.Identifier(APP_ROLE)
            )
        )
    print(f"checkpoint tables ready; runtime role {APP_ROLE} granted")


if __name__ == "__main__":
    asyncio.run(main())
