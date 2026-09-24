import uuid

import httpx
import pytest

from tests.integration.env import TICKET_SERVICE_TOKEN, TICKET_SERVICE_URL

pytestmark = pytest.mark.anyio


@pytest.fixture
def file_ticket(tenant):
    """Creates a ticket directly in ticket-service, the way the agent's create_ticket tool does."""
    try:
        httpx.get(f"{TICKET_SERVICE_URL}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"ticket-service not reachable at {TICKET_SERVICE_URL}: {exc}")

    def _file(requester, summary="VPN 连不上"):
        resp = httpx.post(
            f"{TICKET_SERVICE_URL}/internal/v1/tickets",
            json={"tenant_id": str(tenant.id), "requester_id": str(requester.id), "category": "vpn", "summary": summary},
            headers={"Authorization": f"Bearer {TICKET_SERVICE_TOKEN}", "Idempotency-Key": str(uuid.uuid4())},
        )
        resp.raise_for_status()
        return resp.json()["ticket"]

    return _file


async def act(client, headers, ticket, action, **extra):
    body = {"action": action, "expected_version": ticket["version"], **extra}
    return await client.post(f"/v1/tickets/{ticket['id']}/transitions", json=body, headers=headers)


async def test_employees_see_only_their_own_tickets(client, tenant, login, file_ticket):
    alice, bob, ivan = await login(tenant.alice), await login(tenant.bob), await login(tenant.ivan)
    mine, theirs = file_ticket(tenant.alice, "alice 的工单"), file_ticket(tenant.bob, "bob 的工单")

    # A requester_id filter can't widen an employee's view.
    listed = await client.get("/v1/tickets", params={"requester_id": str(tenant.bob.id)}, headers=alice)
    assert [t["id"] for t in listed.json()["items"]] == [mine["id"]]
    assert (await client.get(f"/v1/tickets/{theirs['id']}", headers=alice)).status_code == 404
    assert (await act(client, alice, theirs, "cancel")).status_code == 404

    everything = (await client.get("/v1/tickets", headers=ivan)).json()["items"]
    assert {t["id"] for t in everything} == {mine["id"], theirs["id"]}
    assert (await client.get(f"/v1/tickets/{theirs['id']}", headers=bob)).json()["ticket"]["id"] == theirs["id"]


async def test_lifecycle_through_the_bff(client, tenant, login, file_ticket):
    alice, ivan = await login(tenant.alice), await login(tenant.ivan)
    ticket = file_ticket(tenant.alice)

    refused = await act(client, alice, ticket, "start")
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "NOT_IT_STAFF")

    ticket = (await act(client, ivan, ticket, "start")).json()
    assert (ticket["status"], ticket["assignee_id"]) == ("in_progress", str(tenant.ivan.id))  # start = take it yourself

    no_note = await act(client, ivan, ticket, "resolve")
    assert (no_note.status_code, no_note.json()["error"]["code"]) == (400, "NOTE_REQUIRED")  # ticket-service's rule
    resolved = (await act(client, ivan, ticket, "resolve", note="已重置 VPN 证书")).json()

    stale = await act(client, alice, ticket, "close")  # still holds the version before resolve
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "VERSION_CONFLICT")
    closed = (await act(client, alice, resolved, "close")).json()
    assert closed["status"] == "closed"

    events = (await client.get(f"/v1/tickets/{ticket['id']}", headers=alice)).json()["events"]
    assert [(e["action"], e["actor_id"]) for e in events] == [
        ("create", str(tenant.alice.id)),
        ("start", str(tenant.ivan.id)),
        ("resolve", str(tenant.ivan.id)),
        ("close", str(tenant.alice.id)),
    ]


async def test_it_staff_cannot_close_on_the_requesters_behalf(client, tenant, login, file_ticket):
    ivan = await login(tenant.ivan)
    ticket = file_ticket(tenant.alice)
    resp = await act(client, ivan, ticket, "cancel")
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "NOT_REQUESTER")
