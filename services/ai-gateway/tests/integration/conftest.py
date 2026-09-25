"""Integration tests against Postgres with the ai_gateway database migrated
(`docker compose up -d ai-gateway`). Skipped when it isn't reachable."""

import uuid

import psycopg
import pytest

from app.core.config import get_settings
from app.db.session import session_factory
from tests.integration.env import ADMIN_DB_URL, DATABASE_URL


@pytest.fixture(scope="session")
def admin_db():
    try:
        conn = psycopg.connect(ADMIN_DB_URL, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"ai_gateway database not reachable: {exc}")
    with conn:
        yield conn


@pytest.fixture
async def settings(monkeypatch, admin_db):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    get_settings.cache_clear()
    session_factory.cache_clear()  # the engine belongs to the previous test's event loop
    yield get_settings()
    await session_factory().kw["bind"].dispose()
    session_factory.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
def tenant(admin_db):
    """A fresh tenant id; its usage rows and budget override are removed afterwards."""
    tenant_id = str(uuid.uuid4())
    yield tenant_id
    admin_db.execute("DELETE FROM model_call_logs WHERE tenant_id = %s", (tenant_id,))
    admin_db.execute("DELETE FROM tenant_token_budgets WHERE tenant_id = %s", (tenant_id,))
