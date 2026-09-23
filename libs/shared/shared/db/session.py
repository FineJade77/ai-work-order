import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_admin_engine: Engine | None = None
_app_engine: Engine | None = None


def admin_session_factory() -> sessionmaker:
    """Sessions on this factory run as the migration/superuser role and bypass RLS."""
    global _admin_engine
    if _admin_engine is None:
        _admin_engine = create_engine(os.environ["DATABASE_URL"])
    return sessionmaker(bind=_admin_engine, expire_on_commit=False)


def app_session_factory() -> sessionmaker:
    """Sessions on this factory run as the non-superuser app role, so RLS policies apply."""
    global _app_engine
    if _app_engine is None:
        _app_engine = create_engine(os.environ["APP_DATABASE_URL"])
    return sessionmaker(bind=_app_engine, expire_on_commit=False)


@contextmanager
def tenant_scope(session: Session, tenant_id: str) -> Iterator[Session]:
    """Scopes every query on this session's current transaction to one tenant via RLS.

    Uses set_config(..., is_local=true) rather than a literal SET statement so the
    tenant id is passed as a bound parameter instead of being interpolated into SQL.
    """
    session.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
    yield session
