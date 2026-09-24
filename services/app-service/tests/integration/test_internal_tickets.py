import uuid

import httpx

from tests.integration.env import APP_SERVICE_URL


def create(client, tenant, *, key, conversation_id=None, summary="VPN 仍然连不上"):
    return client.post(
        "/internal/v1/tickets",
        json={
            "tenant_id": tenant.id,
            "conversation_id": conversation_id or tenant.conversation_id,
            "summary": summary,
            "category": "vpn",
        },
        headers={"Idempotency-Key": key},
    )


def test_requires_service_token(tenants):
    resp = httpx.post(f"{APP_SERVICE_URL}/internal/v1/tickets", json={}, headers={"Idempotency-Key": "k"})
    assert resp.status_code == 401


def test_create_then_replay_returns_the_same_ticket(client, db, tenants):
    tenant = tenants[0]
    key = str(uuid.uuid4())
    first = create(client, tenant, key=key)
    replay = create(client, tenant, key=key)
    assert (first.status_code, first.json()["created"]) == (201, True)
    assert (replay.status_code, replay.json()["created"]) == (200, False)
    assert replay.json()["ticket_id"] == first.json()["ticket_id"]
    assert db.execute("SELECT count(*) FROM tickets WHERE idempotency_key = %s", (key,)).fetchone()[0] == 1


def test_same_key_in_another_tenant_is_a_different_ticket(client, tenants):
    key = str(uuid.uuid4())
    a = create(client, tenants[0], key=key).json()
    b = create(client, tenants[1], key=key).json()
    assert a["created"] and b["created"] and a["ticket_id"] != b["ticket_id"]


def test_unknown_conversation_is_404(client, tenants):
    assert create(client, tenants[0], key=str(uuid.uuid4()), conversation_id=str(uuid.uuid4())).status_code == 404


def test_other_tenants_conversation_is_invisible(client, db, tenants):
    """RLS check: tenant A cannot attach a ticket to tenant B's conversation."""
    key = str(uuid.uuid4())
    resp = create(client, tenants[0], key=key, conversation_id=tenants[1].conversation_id)
    assert resp.status_code == 404
    assert db.execute("SELECT count(*) FROM tickets WHERE idempotency_key = %s", (key,)).fetchone()[0] == 0


def test_invalid_tenant_is_400(client, tenants):
    resp = client.post(
        "/internal/v1/tickets",
        json={"tenant_id": "nope", "conversation_id": tenants[0].conversation_id, "summary": "x", "category": "vpn"},
        headers={"Idempotency-Key": "k"},
    )
    assert resp.status_code == 400
