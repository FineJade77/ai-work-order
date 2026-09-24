import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

from tests.integration.env import GATEWAY_URL, ONCALL_ANSWER, ROLE, SERVICE_TOKEN

TICKET_ARGS = {"summary": "VPN 仍然连不上，已重启客户端", "category": "vpn"}


def test_v1_requires_service_token(tenant):
    resp = httpx.get(f"{GATEWAY_URL}/v1/tools/list", params={"tenant_id": tenant.id, "agent_role": ROLE})
    assert resp.status_code == 401


def test_list_returns_bound_tools_without_internals(gateway, tenant):
    body = gateway.get("/v1/tools/list", params={"tenant_id": tenant.id, "agent_role": ROLE}).json()
    assert [t["function"]["name"] for t in body["tools"]] == ["create_ticket", "search_knowledge"]
    serialized = json.dumps(body).lower()
    assert not any(word in serialized for word in ("handler", "http://", "https://", "password"))


def test_unbound_role_sees_no_tools(gateway, tenant):
    body = gateway.get("/v1/tools/list", params={"tenant_id": tenant.id, "agent_role": "guest"}).json()
    assert body == {"tools": []}


def test_policy_errors_are_returned_to_the_model(call):
    cases = [
        (call("drop_database", {}), "UNKNOWN_TOOL"),
        (call("create_ticket", TICKET_ARGS, key=str(uuid.uuid4()), role="guest"), "TOOL_NOT_ALLOWED"),
        (call("create_ticket", {"summary": "x", "category": "printer"}, key=str(uuid.uuid4())), "INVALID_ARGUMENTS"),
    ]
    for resp, code in cases:
        assert resp.status_code == 200
        assert (resp.json()["status"], resp.json()["error"]["code"]) == ("error", code)


def test_missing_idempotency_key_is_a_caller_error(call):
    resp = call("create_ticket", TICKET_ARGS)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_search_returns_contact_details_unmasked(call):
    body = call("search_knowledge", {"query": "值班"}).json()
    assert body["status"] == "success"
    assert body["result"]["results"][0]["answer"] == ONCALL_ANSWER


def test_read_only_calls_are_not_recorded(call, db, tenant):
    call("search_knowledge", {"query": "VPN"})
    count = db.execute(
        "SELECT count(*) FROM tool_calls WHERE tenant_id = %s AND tool_name = 'search_knowledge'", (tenant.id,)
    ).fetchone()[0]
    assert count == 0


def test_retry_replays_and_reused_key_with_new_payload_conflicts(call):
    key = str(uuid.uuid4())
    first = call("create_ticket", TICKET_ARGS, key=key).json()
    retry = call("create_ticket", TICKET_ARGS, key=key).json()
    assert first["status"] == "success"
    assert retry["replayed"] and retry["result"] == first["result"] and retry["call_id"] == first["call_id"]

    conflict = call("create_ticket", {"summary": "different", "category": "vpn"}, key=key)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_concurrent_requests_with_one_key_create_one_ticket(call, app_db, tenant):
    key = str(uuid.uuid4())

    def attempt(_):
        headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        with httpx.Client(base_url=GATEWAY_URL, headers=headers, timeout=10) as client:
            return call("create_ticket", TICKET_ARGS, key=key, client=client)

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(attempt, range(12)))

    ticket_ids = {r.json()["result"]["ticket_id"] for r in responses if r.status_code == 200}
    assert len(ticket_ids) == 1
    assert all(r.status_code == 200 or r.json()["error"]["code"] == "IN_PROGRESS" for r in responses)
    rows = app_db.execute("SELECT count(*) FROM tickets WHERE idempotency_key = %s", (key,)).fetchone()[0]
    assert rows == 1


def test_orphaned_running_call_recovers_without_duplicate_ticket(call, db, app_db):
    key = str(uuid.uuid4())
    first = call("create_ticket", TICKET_ARGS, key=key).json()

    # Simulate a replica that crashed after app-service created the ticket but before the
    # gateway recorded success. The retry re-sends the same key to app-service.
    db.execute("UPDATE tool_calls SET status = 'running' WHERE idempotency_key = %s", (key,))
    assert call("create_ticket", TICKET_ARGS, key=key).json()["error"]["code"] == "IN_PROGRESS"

    db.execute("UPDATE tool_calls SET updated_at = now() - interval '2 minutes' WHERE idempotency_key = %s", (key,))
    recovered = call("create_ticket", TICKET_ARGS, key=key).json()
    assert recovered["status"] == "success" and recovered["result"] == first["result"]
    assert app_db.execute("SELECT count(*) FROM tickets WHERE idempotency_key = %s", (key,)).fetchone()[0] == 1


def test_audit_masks_pii_in_arguments(call, db, tenant):
    call("create_ticket", {"summary": "请回电 13812345678", "category": "other"}, key=str(uuid.uuid4()))
    summaries = [
        row[0]
        for row in db.execute(
            "SELECT params_summary FROM tool_call_logs WHERE tenant_id = %s AND tool_name = 'create_ticket'",
            (tenant.id,),
        ).fetchall()
    ]
    assert any("138****5678" in s for s in summaries)
    assert not any("13812345678" in s for s in summaries)
