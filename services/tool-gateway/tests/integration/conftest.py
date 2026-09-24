"""Integration tests against a running stack (`docker compose up -d tool-gateway`, which also
starts knowledge-service and ticket-service). Skipped automatically when the gateway isn't up."""

import uuid
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from tests.integration.calls import make_call
from tests.integration.env import (
    DEMO_TENANT,
    GATEWAY_ADMIN_DB_URL,
    GATEWAY_URL,
    ROLE,
    SERVICE_TOKEN,
    TICKET_ADMIN_DB_URL,
)

TOOLS = ("search_knowledge", "create_ticket")


@pytest.fixture(scope="session")
def gateway():
    try:
        httpx.get(f"{GATEWAY_URL}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"tool-gateway not reachable at {GATEWAY_URL}: {exc}")
    with httpx.Client(base_url=GATEWAY_URL, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}, timeout=10) as c:
        yield c


@pytest.fixture(scope="session")
def db():
    with psycopg.connect(GATEWAY_ADMIN_DB_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def ticket_db():
    with psycopg.connect(TICKET_ADMIN_DB_URL, autocommit=True) as conn:
        yield conn


def _bind(db, tenant_id):
    for tool in TOOLS:
        db.execute(
            "INSERT INTO role_tool_bindings (tenant_id, agent_role, tool_name) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (tenant_id, ROLE, tool),
        )


@pytest.fixture(scope="session")
def tenant(db, ticket_db):
    """A throwaway tenant (no knowledge data) whose rows are all removed afterwards."""
    t = SimpleNamespace(id=str(uuid.uuid4()), user=str(uuid.uuid4()), session=str(uuid.uuid4()))
    _bind(db, t.id)
    yield t
    for table in ("role_tool_bindings", "tool_calls", "tool_call_logs"):
        db.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (t.id,))
    ticket_db.execute("DELETE FROM tickets WHERE tenant_id = %s", (t.id,))


@pytest.fixture(scope="session")
def demo(db):
    """The demo tenant, which has static knowledge. Only this run's session rows are removed."""
    t = SimpleNamespace(id=DEMO_TENANT, user=str(uuid.uuid4()), session=str(uuid.uuid4()))
    _bind(db, t.id)
    yield t
    for table in ("tool_calls", "tool_call_logs"):
        db.execute(f"DELETE FROM {table} WHERE session_id = %s", (t.session,))


@pytest.fixture
def call(gateway, tenant):
    return make_call(gateway, tenant)


@pytest.fixture
def demo_call(gateway, demo):
    return make_call(gateway, demo)
