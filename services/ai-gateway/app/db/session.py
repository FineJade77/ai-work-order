import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@lru_cache
def session_factory() -> async_sessionmaker[AsyncSession]:
    """Runtime sessions, as the non-owner app account, so RLS policies apply."""
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_scope(session: AsyncSession, tenant_id: str) -> AsyncIterator[AsyncSession]:
    """Scopes every query in the session's current transaction to one tenant via RLS.

    set_config(..., is_local=true) lasts until the transaction ends. It is used instead of a
    literal SET so the tenant id is a bound parameter rather than interpolated SQL.
    """
    await session.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
    yield session
