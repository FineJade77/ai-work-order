import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker


@lru_cache
def session_factory() -> sessionmaker:
    """Runtime sessions, as the non-owner app account, so RLS policies apply."""
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def tenant_scope(session: Session, tenant_id: str) -> Iterator[Session]:
    """Scopes every query in the session's current transaction to one tenant via RLS.

    set_config(..., is_local=true) lasts until the transaction ends. It is used instead of a
    literal SET so the tenant id is a bound parameter rather than interpolated SQL.
    """
    session.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
    yield session
