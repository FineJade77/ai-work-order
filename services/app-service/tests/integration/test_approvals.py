"""Approvals through app-service: decision events become approval turns (the app's own consumer,
running in its lifespan), and handlers list and decide approvals held by a running tool-gateway."""

import uuid

import anyio
import httpx
import psycopg
import pytest

from app.clients.agent_bus import task_stream
from tests.integration.env import GATEWAY_ADMIN_DB_URL, TOOL_GATEWAY_TOKEN, TOOL_GATEWAY_URL
from tests.integration.test_conversations import history, report, send, start_conversation, wait_for

pytestmark = pytest.mark.anyio

GRANT = {"system": "财务系统", "access_level": "read", "reason": "月底对账"}


async def decision_event(redis, tenant, conversation, *, status="approved", approval_id=None, note="") -> str:
    """What tool-gateway's relay publishes."""
    approval_id = approval_id or str(uuid.uuid4())
    await redis.xadd("approval_events", {
        "approval_id": approval_id, "tenant_id": str(tenant.id), "conversation_id": conversation,
        "requester_id": str(tenant.alice.id), "tool_name": "grant_access", "status": status, "decided_by": "",
        "note": note, "approval_traceparent": "",
    })
    return approval_id


async def approval_tasks(redis) -> list[dict]:
    streams = await redis.keys(task_stream("*"))  # the dedicated test database: only this test's tenant
    return [f for stream in streams for _, f in await redis.xrange(stream) if f.get("kind") == "approval"]


async def test_a_decision_opens_an_approval_turn_that_resumes_the_run(client, redis, tenant, login, admin_db):
    alice = await login(tenant.alice)
    conversation = await start_conversation(client, alice)
    first = (await send(client, alice, conversation)).json()["task_id"]
    await report(redis, tenant, conversation, first, status="done", outcome="awaiting_approval",
                 content="已提交审批", ticket_id="")
    await wait_for(lambda: _history_when(client, alice, conversation, lambda h: h["active_task_id"] is None))

    approval_id = await decision_event(redis, tenant, conversation, note="已核实")
    [task] = await wait_for(lambda: approval_tasks(redis))
    assert (task["approval_id"], task["decision"], task["note"]) == (approval_id, "approved", "已核实")
    assert task["content"] == ""
    assert (task["seq"], task["user_id"]) == ("2", str(tenant.alice.id))
    kind, decision = admin_db.execute(
        "SELECT kind, approval_decision FROM turns WHERE id = %s", (task["task_id"],)
    ).fetchone()
    assert (kind, decision) == ("approval", "approved")

    # Redelivered by the relay (at least once): no second turn, no second task.
    await decision_event(redis, tenant, conversation, approval_id=approval_id)
    await anyio.sleep(1)
    assert len(await approval_tasks(redis)) == 1

    # The agent's reply to the approval turn lands in the conversation like any other.
    await report(redis, tenant, conversation, task["task_id"], status="done", outcome="action_done",
                 content="已为你办理完成", ticket_id="")
    done = await wait_for(lambda: _history_when(client, alice, conversation, lambda h: h["active_task_id"] is None))
    assert [m["content"] for m in done["messages"]][-1] == "已为你办理完成"


async def test_a_decision_waits_for_the_turn_in_flight(client, redis, tenant, login):
    alice = await login(tenant.alice)
    conversation = await start_conversation(client, alice)
    in_flight = (await send(client, alice, conversation)).json()["task_id"]
    approval_id = await decision_event(redis, tenant, conversation)
    await anyio.sleep(1.5)
    assert await approval_tasks(redis) == []  # one queued turn per conversation: the event stays pending

    await report(redis, tenant, conversation, in_flight, status="done", outcome="replied", content="好的", ticket_id="")
    await wait_for(lambda: _history_when(client, alice, conversation, lambda h: h["active_task_id"] is None))
    # Redelivered once idle for CLAIM_IDLE_MS (30 s): it then opens its turn.
    [task] = await wait_for(lambda: approval_tasks(redis), timeout=60)
    assert task["approval_id"] == approval_id


async def test_an_empty_result_closes_the_turn_without_a_reply(client, redis, tenant, login, admin_db):
    alice = await login(tenant.alice)
    conversation = await start_conversation(client, alice)
    await decision_event(redis, tenant, conversation)
    [task] = await wait_for(lambda: approval_tasks(redis))
    await report(
        redis, tenant, conversation, task["task_id"], status="done", outcome="ignored", content="", ticket_id=""
    )
    await wait_for(lambda: _turn_status(admin_db, task["task_id"], "done"))
    assert (await history(client, alice, conversation))["messages"] == []


@pytest.fixture
async def gateway_approval(tenant):
    """A real pending approval in tool-gateway: the test tenant's grant_access needs approval, and
    Alice (through the agent, here called directly) asked for it."""
    try:
        httpx.get(f"{TOOL_GATEWAY_URL}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"tool-gateway not reachable at {TOOL_GATEWAY_URL}: {exc}")
    with psycopg.connect(GATEWAY_ADMIN_DB_URL, autocommit=True) as db:
        db.execute(
            "INSERT INTO role_tool_bindings (tenant_id, agent_role, tool_name, requires_approval)"
            " VALUES (%s, 'service_agent', 'grant_access', true)",
            (tenant.id,),
        )
        body = {
            "tenant_id": str(tenant.id), "session_id": str(uuid.uuid4()), "user_id": str(tenant.alice.id),
            "agent_role": "service_agent", "tool_name": "grant_access", "arguments": GRANT,
        }
        resp = httpx.post(
            f"{TOOL_GATEWAY_URL}/v1/tools/call", json=body,
            headers={"Authorization": f"Bearer {TOOL_GATEWAY_TOKEN}", "Idempotency-Key": str(uuid.uuid4())},
        )
        yield resp.json()["approval"]["id"]
        for table in ("approval_outbox", "tool_approvals", "access_grants", "tool_call_logs", "tool_calls",
                      "role_tool_bindings"):
            db.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant.id,))


async def test_handlers_decide_and_employees_only_see_their_own(client, tenant, login, gateway_approval):
    alice, bob, ivan = await login(tenant.alice), await login(tenant.bob), await login(tenant.ivan)
    [mine] = (await client.get("/v1/approvals", headers=alice)).json()["items"]
    assert (mine["id"], mine["arguments"], mine["status"]) == (gateway_approval, GRANT, "pending")
    assert (await client.get("/v1/approvals", headers=bob)).json()["items"] == []
    assert (await client.get(f"/v1/approvals/{gateway_approval}", headers=bob)).status_code == 404
    assert [a["id"] for a in (await client.get("/v1/approvals", headers=ivan)).json()["items"]] == [gateway_approval]

    refused = await client.post(f"/v1/approvals/{gateway_approval}/approve", json={}, headers=alice)
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "NOT_HANDLER")
    approved = await client.post(f"/v1/approvals/{gateway_approval}/approve", json={"note": "已核实"}, headers=ivan)
    assert (approved.status_code, approved.json()["status"], approved.json()["decided_by"]) == (
        200, "approved", str(tenant.ivan.id),
    )
    again = await client.post(f"/v1/approvals/{gateway_approval}/reject", json={}, headers=ivan)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "APPROVAL_NOT_PENDING")  # tool-gateway's


async def _history_when(client, headers, conversation, predicate):
    h = await history(client, headers, conversation)
    return h if predicate(h) else None


async def _turn_status(admin_db, task_id, status):
    row = admin_db.execute("SELECT status FROM turns WHERE id = %s", (task_id,)).fetchone()
    return row and row[0] == status
