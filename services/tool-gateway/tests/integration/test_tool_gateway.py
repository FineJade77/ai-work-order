import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

from tests.integration.calls import make_call
from tests.integration.env import GATEWAY_URL, ONCALL_ANSWER, ROLE, SERVICE_TOKEN

TICKET_ARGS = {"summary": "VPN 仍然连不上，已重启客户端", "category": "vpn"}


def test_v1_requires_service_token(tenant):
    resp = httpx.get(f"{GATEWAY_URL}/v1/tools/list", params={"tenant_id": tenant.id, "agent_role": ROLE})
    assert resp.status_code == 401


def test_list_returns_bound_tools_without_internals(gateway, tenant):
    body = gateway.get("/v1/tools/list", params={"tenant_id": tenant.id, "agent_role": ROLE}).json()
    assert [t["function"]["name"] for t in body["tools"]] == ["create_ticket", "search_knowledge"]
    serialized = json.dumps(body).lower()
    assert not any(word in serialized for word in ("handler", "http://", "https://", "password", "token"))


def test_unbound_role_sees_no_tools(gateway, tenant):
    body = gateway.get("/v1/tools/list", params={"tenant_id": tenant.id, "agent_role": "guest"}).json()
    assert body == {"tools": []}


def test_policy_errors_are_returned_to_the_model(call):
    cases = [
        (call("drop_database", {}), "UNKNOWN_TOOL"),
        (call("create_ticket", TICKET_ARGS, key=str(uuid.uuid4()), role="guest"), "TOOL_NOT_ALLOWED"),
        (call("create_ticket", {"summary": "x", "category": ""}, key=str(uuid.uuid4())), "INVALID_ARGUMENTS"),
    ]
    for resp, code in cases:
        assert resp.status_code == 200
        assert (resp.json()["status"], resp.json()["error"]["code"]) == ("error", code)


def test_caller_contract_violations_are_http_errors(call):
    missing_key = call("create_ticket", TICKET_ARGS)
    assert (missing_key.status_code, missing_key.json()["error"]["code"]) == (400, "MISSING_IDEMPOTENCY_KEY")
    bad_user = call("search_knowledge", {"query": "VPN"}, user="not-a-uuid")
    assert (bad_user.status_code, bad_user.json()["error"]["code"]) == (400, "INVALID_USER")


def test_search_goes_to_knowledge_service_for_the_callers_tenant(demo_call, call):
    results = demo_call("search_knowledge", {"query": "VPN 连不上"}).json()["result"]["results"]
    assert (results[0]["question"], results[0]["category"]) == ("VPN 连不上怎么办？", "vpn")
    # The throwaway tenant has no knowledge data: same query, nothing found.
    assert call("search_knowledge", {"query": "VPN 连不上"}).json()["result"]["results"] == []


def test_search_returns_contact_details_unmasked(demo_call):
    results = demo_call("search_knowledge", {"query": "值班"}).json()["result"]["results"]
    assert results[0]["answer"] == ONCALL_ANSWER


def test_read_only_calls_are_not_recorded(call, db, tenant):
    call("search_knowledge", {"query": "VPN"})
    count = db.execute(
        "SELECT count(*) FROM tool_calls WHERE tenant_id = %s AND tool_name = 'search_knowledge'", (tenant.id,)
    ).fetchone()[0]
    assert count == 0


def test_create_ticket_lands_in_ticket_service_with_the_employee_as_requester(call, ticket_db, tenant):
    key = str(uuid.uuid4())
    body = call("create_ticket", TICKET_ARGS, key=key).json()
    assert body["status"] == "success"
    row = ticket_db.execute(
        "SELECT id, requester_id, conversation_id, status FROM tickets WHERE idempotency_key = %s", (key,)
    ).fetchone()
    assert str(row[0]) == body["result"]["ticket_id"]
    assert (str(row[1]), str(row[2]), row[3]) == (tenant.user, tenant.session, "open")


def test_retry_replays_and_reused_key_with_new_payload_conflicts(call):
    key = str(uuid.uuid4())
    first = call("create_ticket", TICKET_ARGS, key=key).json()
    retry = call("create_ticket", TICKET_ARGS, key=key).json()
    assert first["status"] == "success"
    assert retry["replayed"] and retry["result"] == first["result"] and retry["call_id"] == first["call_id"]

    conflict = call("create_ticket", {"summary": "different", "category": "vpn"}, key=key)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_concurrent_requests_with_one_key_create_one_ticket(ticket_db, tenant):
    key = str(uuid.uuid4())

    def attempt(_):
        headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        with httpx.Client(base_url=GATEWAY_URL, headers=headers, timeout=10) as client:
            return make_call(client, tenant)("create_ticket", TICKET_ARGS, key=key)

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(attempt, range(12)))

    ticket_ids = {r.json()["result"]["ticket_id"] for r in responses if r.status_code == 200}
    assert len(ticket_ids) == 1
    assert all(r.status_code == 200 or r.json()["error"]["code"] == "IN_PROGRESS" for r in responses)
    rows = ticket_db.execute("SELECT count(*) FROM tickets WHERE idempotency_key = %s", (key,)).fetchone()[0]
    assert rows == 1


def test_orphaned_running_call_recovers_without_duplicate_ticket(call, db, ticket_db):
    key = str(uuid.uuid4())
    first = call("create_ticket", TICKET_ARGS, key=key).json()

    # Simulate a replica that crashed after ticket-service created the ticket but before the
    # gateway recorded success. The retry re-sends the same key to ticket-service.
    db.execute("UPDATE tool_calls SET status = 'running' WHERE idempotency_key = %s", (key,))
    assert call("create_ticket", TICKET_ARGS, key=key).json()["error"]["code"] == "IN_PROGRESS"

    db.execute("UPDATE tool_calls SET updated_at = now() - interval '2 minutes' WHERE idempotency_key = %s", (key,))
    recovered = call("create_ticket", TICKET_ARGS, key=key).json()
    assert recovered["status"] == "success" and recovered["result"] == first["result"]
    assert ticket_db.execute("SELECT count(*) FROM tickets WHERE idempotency_key = %s", (key,)).fetchone()[0] == 1


def test_audit_masks_pii_and_records_the_employee(call, db, tenant):
    key = str(uuid.uuid4())
    call("create_ticket", {"summary": "请回电 13812345678", "category": "other"}, key=key)
    rows = db.execute(
        "SELECT params_summary, user_id FROM tool_call_logs WHERE tenant_id = %s AND tool_name = 'create_ticket'",
        (tenant.id,),
    ).fetchall()
    summaries = [r[0] for r in rows]
    assert any("138****5678" in s for s in summaries)
    assert not any("13812345678" in s for s in summaries)
    assert {str(r[1]) for r in rows} == {tenant.user}


def test_invalid_user_rejection_is_still_audited(call, db, tenant):
    call("search_knowledge", {"query": "审计"}, user="not-a-uuid")
    row = db.execute(
        "SELECT status, user_id FROM tool_call_logs WHERE tenant_id = %s AND error_code = 'INVALID_USER'",
        (tenant.id,),
    ).fetchone()
    assert row == ("rejected", None)
