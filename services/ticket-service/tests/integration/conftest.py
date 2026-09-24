"""Integration tests against a running ticket-service (`docker compose up -d ticket-service`).
Skipped automatically when it isn't reachable."""

import uuid
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from tests.integration.env import ADMIN_DB_URL, SERVICE_TOKEN, TICKET_SERVICE_URL


@pytest.fixture(scope="session")
def client():
    try:
        httpx.get(f"{TICKET_SERVICE_URL}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"ticket-service not reachable at {TICKET_SERVICE_URL}: {exc}")
    with httpx.Client(base_url=TICKET_SERVICE_URL, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}) as c:
        yield c


@pytest.fixture(scope="session")
def db():
    with psycopg.connect(ADMIN_DB_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture
def tenant(db):
    """A fresh tenant with one employee and one IT staff member; its tickets are deleted afterwards."""
    t = SimpleNamespace(id=str(uuid.uuid4()), requester=str(uuid.uuid4()), staff=str(uuid.uuid4()))
    yield t
    db.execute("DELETE FROM tickets WHERE tenant_id = %s", (t.id,))  # cascades to ticket_events


@pytest.fixture
def create(client, tenant):
    def _create(summary="VPN 连不上", key=None, requester=None):
        return client.post(
            "/internal/v1/tickets",
            json={
                "tenant_id": tenant.id,
                "requester_id": requester or tenant.requester,
                "conversation_id": str(uuid.uuid4()),
                "category": "vpn",
                "summary": summary,
            },
            headers={"Idempotency-Key": key or str(uuid.uuid4())},
        )

    return _create


@pytest.fixture
def act(client, tenant):
    def _act(ticket, action, actor, **extra):
        body = {"tenant_id": tenant.id, "action": action, "actor_id": actor, "expected_version": ticket["version"]}
        body.update(extra)
        return client.post(f"/internal/v1/tickets/{ticket['id']}/transitions", json=body)

    return _act
