import json
import uuid

import anyio
import pytest

from app.clients.agent_bus import RESULT_STREAM, TASK_STREAM, AgentBus, event_stream_key
from app.core.config import get_settings
from app.workers.requeuer import NO_RESULT_MESSAGE, Requeuer
from tests.integration.env import PASSWORD

pytestmark = pytest.mark.anyio


async def start_conversation(client, headers) -> str:
    resp = await client.post("/v1/conversations", headers=headers)
    assert resp.status_code == 201
    return resp.json()["id"]


async def send(client, headers, conversation_id, content="VPN 连不上"):
    return await client.post(f"/v1/conversations/{conversation_id}/messages", json={"content": content}, headers=headers)


async def history(client, headers, conversation_id) -> dict:
    return (await client.get(f"/v1/conversations/{conversation_id}/messages", headers=headers)).json()


async def report(redis, tenant, conversation_id, task_id, **fields):
    """What agent-runtime writes when a task ends."""
    base = {"task_id": task_id, "conversation_id": conversation_id, "tenant_id": str(tenant.id)}
    await redis.xadd(RESULT_STREAM, {**base, **fields})


async def wait_for(predicate, timeout=10):
    with anyio.fail_after(timeout):
        while not (result := await predicate()):
            await anyio.sleep(0.1)
        return result


async def test_login_and_profile(client, tenant, login):
    headers = await login(tenant.ivan)
    me = (await client.get("/v1/me", headers=headers)).json()
    assert (me["email"], me["role"], me["tenant_id"]) == (tenant.ivan.email, "it_staff", str(tenant.id))


@pytest.mark.parametrize("field", ["tenant", "email", "password"])
async def test_every_login_failure_looks_the_same(client, tenant, field):
    body = {"tenant": tenant.slug, "email": tenant.alice.email, "password": PASSWORD, field: "wrong"}
    resp = await client.post("/v1/auth/login", json=body)
    assert (resp.status_code, resp.json()["error"]["code"]) == (401, "INVALID_CREDENTIALS")


async def test_conversations_are_private_to_their_owner(client, tenant, login):
    alice, bob, ivan = await login(tenant.alice), await login(tenant.bob), await login(tenant.ivan)
    conversation = await start_conversation(client, alice)
    assert [c["id"] for c in (await client.get("/v1/conversations", headers=alice)).json()["items"]] == [conversation]
    assert (await client.get("/v1/conversations", headers=bob)).json()["items"] == []
    for headers in (bob, ivan):
        assert (await client.get(f"/v1/conversations/{conversation}/messages", headers=headers)).status_code == 404
        assert (await send(client, headers, conversation)).status_code == 404


async def test_send_enqueues_a_task_and_refuses_a_second_while_one_is_in_flight(client, redis, tenant, login):
    alice = await login(tenant.alice)
    conversation = await start_conversation(client, alice)
    resp = await send(client, alice, conversation)
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]

    [(_, task)] = await redis.xrange(TASK_STREAM)
    assert task == {
        "task_id": task_id, "conversation_id": conversation, "tenant_id": str(tenant.id),
        "user_id": str(tenant.alice.id), "content": "VPN 连不上",
    }
    second = await send(client, alice, conversation, "还有一个问题")
    assert (second.status_code, second.json()["error"]["code"]) == (409, "TURN_IN_PROGRESS")
    assert (await history(client, alice, conversation))["active_task_id"] == task_id


async def test_result_is_stored_once_and_frees_the_conversation(client, redis, tenant, login):
    alice = await login(tenant.alice)
    conversation = await start_conversation(client, alice)
    task_id = (await send(client, alice, conversation)).json()["task_id"]

    for _ in range(2):  # at-least-once delivery: the same result twice
        await report(redis, tenant, conversation, task_id, status="done", outcome="awaiting_feedback",
                     content="请重启 VPN 客户端。", ticket_id="")
    done = await wait_for(lambda: _history_when(client, alice, conversation, lambda h: h["active_task_id"] is None))
    assert [(m["role"], m["content"], m["task_id"]) for m in done["messages"]] == [
        ("user", "VPN 连不上", task_id),
        ("assistant", "请重启 VPN 客户端。", task_id),
    ]
    assert (await send(client, alice, conversation, "还是不行")).status_code == 202


async def test_late_answer_replaces_a_failure_notice(client, redis, tenant, login, admin_db):
    alice = await login(tenant.alice)
    conversation = await start_conversation(client, alice)
    task_id = (await send(client, alice, conversation)).json()["task_id"]
    ticket_id = str(uuid.uuid4())

    await report(redis, tenant, conversation, task_id, status="failed", error_code="TASK_FAILED", content="处理失败")
    await wait_for(lambda: _history_when(client, alice, conversation, lambda h: len(h["messages"]) == 2))
    await report(redis, tenant, conversation, task_id, status="done", outcome="ticket_created",
                 content="已为你创建 IT 工单", ticket_id=ticket_id)
    final = await wait_for(
        lambda: _history_when(client, alice, conversation, lambda h: h["messages"][-1]["content"] == "已为你创建 IT 工单")
    )
    assert len(final["messages"]) == 2
    row = admin_db.execute("SELECT status, outcome, ticket_id::text FROM turns WHERE id = %s", (task_id,)).fetchone()
    assert row == ("done", "ticket_created", ticket_id)


async def test_stream_relays_this_turns_events_and_resumes_after_last_event_id(client, redis, tenant, login):
    alice = await login(tenant.alice)
    conversation = await start_conversation(client, alice)
    task_id = (await send(client, alice, conversation)).json()["task_id"]
    key = event_stream_key(conversation)
    await redis.xadd(key, {"task_id": str(uuid.uuid4()), "type": "answer", "data": "{}"})  # another turn's event
    first = await redis.xadd(key, {"task_id": task_id, "type": "retrieving", "data": "{}"})
    await redis.xadd(key, {"task_id": task_id, "type": "answer_chunk", "data": json.dumps({"delta": "请"})})
    await redis.xadd(key, {"task_id": task_id, "type": "done", "data": json.dumps({"outcome": "awaiting_feedback"})})

    url = f"/v1/conversations/{conversation}/stream?task_id={task_id}"
    resp = await client.get(url, headers=alice)
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert _event_types(resp.text) == ["retrieving", "answer_chunk", "done"]
    resumed = await client.get(url, headers={**alice, "Last-Event-ID": first})
    assert _event_types(resumed.text) == ["answer_chunk", "done"]


async def test_requeuer_re_enqueues_a_lost_task_then_gives_up(client, redis, tenant, login, admin_db):
    alice = await login(tenant.alice)
    conversation = await start_conversation(client, alice)
    task_id = (await send(client, alice, conversation)).json()["task_id"]
    settings = get_settings().model_copy(update={"max_requeues": 1})
    requeuer = Requeuer(AgentBus(redis), settings)

    def age_turn():
        admin_db.execute("UPDATE turns SET updated_at = now() - interval '1 hour' WHERE id = %s", (task_id,))

    age_turn()
    await requeuer.sweep()
    tasks = [fields["task_id"] for _, fields in await redis.xrange(TASK_STREAM)]
    assert tasks == [task_id, task_id]  # same task_id: agent-runtime treats it idempotently

    age_turn()
    await requeuer.sweep()
    assert len(await redis.xrange(TASK_STREAM)) == 2  # gave up instead of enqueuing a third time
    h = await history(client, alice, conversation)
    assert h["active_task_id"] is None and h["messages"][-1]["content"] == NO_RESULT_MESSAGE
    [(_, event)] = await redis.xrange(event_stream_key(conversation))
    assert (event["task_id"], event["type"]) == (task_id, "error")


async def _history_when(client, headers, conversation, predicate):
    h = await history(client, headers, conversation)
    return h if predicate(h) else None


def _event_types(body: str) -> list[str]:
    return [line.removeprefix("event: ") for line in body.splitlines() if line.startswith("event: ")]
