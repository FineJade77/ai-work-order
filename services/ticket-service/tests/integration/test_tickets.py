import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

from tests.integration.env import SERVICE_TOKEN, TICKET_SERVICE_URL


def test_create_is_idempotent_and_records_a_create_event(client, tenant, create):
    key = str(uuid.uuid4())
    first, replay = create(key=key), create(key=key)
    assert (first.status_code, first.json()["created"]) == (201, True)
    assert (replay.status_code, replay.json()["created"]) == (200, False)
    ticket = first.json()["ticket"]
    assert replay.json()["ticket"]["id"] == ticket["id"]
    assert (ticket["status"], ticket["version"]) == ("open", 1)

    detail = client.get(f"/internal/v1/tickets/{ticket['id']}", params={"tenant_id": tenant.id}).json()
    assert [(e["action"], e["to_status"]) for e in detail["events"]] == [("create", "open")]


def test_full_lifecycle_records_every_step(client, tenant, create, act):
    t = create().json()["ticket"]
    t = act(t, "start", tenant.staff, assignee_id=tenant.staff).json()
    assert (t["status"], t["assignee_id"], t["version"]) == ("in_progress", tenant.staff, 2)
    t = act(t, "resolve", tenant.staff, note="已重置 VPN 证书").json()
    t = act(t, "reopen", tenant.requester).json()
    assert t["status"] == "open"
    t = act(t, "start", tenant.staff, assignee_id=tenant.staff).json()
    t = act(t, "resolve", tenant.staff, note="更换客户端版本").json()
    t = act(t, "close", tenant.requester).json()
    assert (t["status"], t["version"]) == ("closed", 7)

    events = client.get(f"/internal/v1/tickets/{t['id']}", params={"tenant_id": tenant.id}).json()["events"]
    assert [e["action"] for e in events] == ["create", "start", "resolve", "reopen", "start", "resolve", "close"]
    assert events[2]["note"] == "已重置 VPN 证书"


def test_invalid_transition_and_requester_only_rules(tenant, create, act):
    t = create().json()["ticket"]
    resp = act(t, "resolve", tenant.staff, note="x")
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "INVALID_TRANSITION")
    resp = act(t, "cancel", tenant.staff)
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "NOT_REQUESTER")
    assert act(t, "cancel", tenant.requester).json()["status"] == "closed"


def test_stale_version_is_rejected(tenant, create, act):
    t = create().json()["ticket"]
    act(t, "start", tenant.staff, assignee_id=tenant.staff)
    resp = act(t, "cancel", tenant.requester)  # still holds version 1
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "VERSION_CONFLICT")


def test_concurrent_transitions_on_one_version_have_exactly_one_winner(db, tenant, create):
    t = create().json()["ticket"]
    staff = [str(uuid.uuid4()) for _ in range(8)]

    def start(assignee):
        body = {"tenant_id": tenant.id, "action": "start", "actor_id": assignee,
                "expected_version": 1, "assignee_id": assignee}
        with httpx.Client(base_url=TICKET_SERVICE_URL, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}) as c:
            return c.post(f"/internal/v1/tickets/{t['id']}/transitions", json=body)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(start, staff))
    assert sorted(r.status_code for r in responses) == [200] + [409] * 7
    starts = db.execute(
        "SELECT count(*) FROM ticket_events WHERE ticket_id = %s AND action = 'start'", (t["id"],)
    ).fetchone()[0]
    assert starts == 1


def test_list_filters_and_paginates(client, tenant, create, act):
    other_requester = str(uuid.uuid4())
    mine = [create(summary=f"mine-{i}").json()["ticket"] for i in range(5)]
    create(summary="someone else", requester=other_requester)
    act(mine[0], "cancel", tenant.requester)

    seen, cursor = [], None
    while True:
        params = {"tenant_id": tenant.id, "requester_id": tenant.requester, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/internal/v1/tickets", params=params).json()
        seen += [t["summary"] for t in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [f"mine-{i}" for i in reversed(range(5))]  # newest first, no gaps or repeats

    open_only = client.get(
        "/internal/v1/tickets", params={"tenant_id": tenant.id, "requester_id": tenant.requester, "status": "open"}
    ).json()["items"]
    assert len(open_only) == 4


def test_tickets_are_invisible_to_other_tenants(client, create):
    t = create().json()["ticket"]
    other_tenant = str(uuid.uuid4())
    assert client.get(f"/internal/v1/tickets/{t['id']}", params={"tenant_id": other_tenant}).status_code == 404
    assert client.get("/internal/v1/tickets", params={"tenant_id": other_tenant}).json()["items"] == []
