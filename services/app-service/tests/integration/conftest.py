"""Integration tests against a running app-service (`docker compose up -d app-service`).
Skipped automatically when it isn't reachable."""

import uuid
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from tests.integration.env import ADMIN_DB_URL, APP_SERVICE_URL, INTERNAL_TOKEN


@pytest.fixture(scope="session")
def client():
    try:
        httpx.get(f"{APP_SERVICE_URL}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"app-service not reachable at {APP_SERVICE_URL}: {exc}")
    with httpx.Client(base_url=APP_SERVICE_URL, headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"}) as c:
        yield c


@pytest.fixture(scope="session")
def db():
    with psycopg.connect(ADMIN_DB_URL, autocommit=True) as conn:
        yield conn


def _make_tenant(db) -> SimpleNamespace:
    tenant_id = str(uuid.uuid4())
    db.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_id, f"it-{tenant_id[:8]}"))
    user_id = db.execute(
        "INSERT INTO users (tenant_id, name, email) VALUES (%s, 'Test', 'test@example.com') RETURNING id",
        (tenant_id,),
    ).fetchone()[0]
    conversation_id = str(
        db.execute(
            "INSERT INTO conversations (tenant_id, user_id) VALUES (%s, %s) RETURNING id", (tenant_id, user_id)
        ).fetchone()[0]
    )
    return SimpleNamespace(id=tenant_id, conversation_id=conversation_id)


@pytest.fixture(scope="session")
def tenants(db):
    """Two isolated tenants per run, removed afterwards (tenants cascade to their rows)."""
    pair = [_make_tenant(db), _make_tenant(db)]
    yield pair
    for t in pair:
        db.execute("DELETE FROM tenants WHERE id = %s", (t.id,))
