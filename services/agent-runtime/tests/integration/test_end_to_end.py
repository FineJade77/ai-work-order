"""The real worker loop against Redis, the Postgres checkpointer, tool-gateway, knowledge-service
and ticket-service. Only the model is faked (tests.fakes.FakeModels), so no ai-gateway is needed."""

import asyncio
import json
import uuid

import anyio
import httpx
import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app.clients.tool_gateway import ToolGatewayClient
from app.events import RESULT_STREAM, EventPublisher, event_stream_key
from app.graph import prompts
from app.graph.builder import build_graph
from app.worker.consumer import TaskConsumer
from app.worker.queue import CONSUMER_GROUP, TASK_STREAM, RedisStreamTaskQueue, floor_key
from app.worker.runner import TaskRunner
from tests.fakes import TENANT, FakeModels, make_task
from tests.integration.env import (
    CHECKPOINT_ADMIN_DB_URL,
    CHECKPOINT_DB_URL,
    GATEWAY_ADMIN_DB_URL,
    TICKET_ADMIN_DB_URL,
    TOOL_GATEWAY_TOKEN,
    TOOL_GATEWAY_URL,
)

pytestmark = pytest.mark.anyio


class CrashingTools:
    """The real tool-gateway client, but can die right after create_ticket succeeded downstream:
    the ticket exists, yet the node never completed, so its checkpoint was never written."""

    def __init__(self, inner: ToolGatewayClient):
        self._inner = inner
        self.crash_after_create = 0

    async def list_tools(self, task):
        return await self._inner.list_tools(task)

    async def call(self, task, tool_name, arguments, *, idempotency_key=None):
        result = await self._inner.call(task, tool_name, arguments, idempotency_key=idempotency_key)
        if tool_name == "create_ticket" and self.crash_after_create:
            self.crash_after_create -= 1
            raise RuntimeError("worker died after the ticket was created")
        return result


class Worker:
    def __init__(self, redis, settings, tools, models):
        self.redis, self.settings, self.tools, self.models = redis, settings, tools, models
        self._pool: AsyncConnectionPool | None = None
        self._consumer: asyncio.Task | None = None

    async def start(self):
        """A fresh process's worth of state: new pool, new graph, new consumer."""
        self._pool = AsyncConnectionPool(
            CHECKPOINT_DB_URL, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}, open=False
        )
        await self._pool.open(wait=True, timeout=5)
        runner = TaskRunner(
            build_graph(AsyncPostgresSaver(self._pool)),
            self.tools,
            self.models,
            lambda t: EventPublisher(self.redis, t),
        )
        queue = RedisStreamTaskQueue(
            self.redis, "e2e", claim_idle_ms=int(self.settings.claim_idle_seconds * 1000), claim_interval_s=0.2
        )
        self._consumer = asyncio.create_task(TaskConsumer(queue, runner, self.redis, self.settings).run())

    async def stop(self):
        self._consumer.cancel()
        await asyncio.gather(self._consumer, return_exceptions=True)
        await self._pool.close()

    async def restart(self):
        await self.stop()
        await self.start()


@pytest.fixture
async def worker(redis, settings):
    try:
        httpx.get(f"{TOOL_GATEWAY_URL}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"tool-gateway not reachable at {TOOL_GATEWAY_URL}: {exc}")
    http = httpx.AsyncClient(
        base_url=TOOL_GATEWAY_URL, headers={"Authorization": f"Bearer {TOOL_GATEWAY_TOKEN}"}, timeout=10
    )
    w = Worker(redis, settings, CrashingTools(ToolGatewayClient(http, "service_agent")), FakeModels())
    try:
        await w.start()
    except PoolTimeout as exc:
        await http.aclose()
        pytest.skip(f"agent_runtime database not reachable: {exc}")
    w.conversation, w.user, w.seq = uuid.uuid4(), uuid.uuid4(), 0
    yield w
    await w.stop()
    await http.aclose()
    _cleanup(w.conversation, w.user)


def _cleanup(conversation, user):
    thread = str(conversation)
    with psycopg.connect(CHECKPOINT_ADMIN_DB_URL, autocommit=True) as db:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            db.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread,))
    with psycopg.connect(GATEWAY_ADMIN_DB_URL, autocommit=True) as db:
        db.execute(
            "DELETE FROM approval_outbox WHERE approval_id IN (SELECT id FROM tool_approvals WHERE session_id = %s)",
            (thread,),
        )
        db.execute("DELETE FROM access_grants WHERE idempotency_key LIKE %s", (f"{thread}:%",))
        for table in ("tool_calls", "tool_call_logs"):  # tool_approvals go with their calls (cascade)
            db.execute(f"DELETE FROM {table} WHERE session_id = %s", (thread,))
    with psycopg.connect(TICKET_ADMIN_DB_URL, autocommit=True) as db:
        # By conversation, not tenant: a test may run as another tenant (see the HR test below).
        db.execute("DELETE FROM tickets WHERE conversation_id = %s", (thread,))


def _tickets(conversation):
    with psycopg.connect(TICKET_ADMIN_DB_URL) as db:
        return db.execute(
            "SELECT id, requester_id, category, summary FROM tickets WHERE conversation_id = %s", (str(conversation),)
        ).fetchall()


def task_fields(task) -> dict[str, str]:
    """As app-service writes a task: fields a task doesn't use (None) are left out."""
    return {k: str(v) for k, v in task.model_dump().items() if v is not None}


async def send(w: Worker, content: str, *, tenant_id: uuid.UUID | None = None) -> list[tuple[str, dict]]:
    """Enqueues the conversation's next message the way app-service does (floor raised to its seq
    in the same transaction) and waits for its run's 'done' event."""
    w.seq += 1
    task = make_task(w.conversation, content, user_id=w.user, seq=w.seq)
    if tenant_id:
        task = task.model_copy(update={"tenant_id": tenant_id})
    w.last_task = task
    async with w.redis.pipeline(transaction=True) as pipe:
        pipe.set(floor_key(w.conversation), task.seq)
        pipe.xadd(TASK_STREAM, task_fields(task))
        await pipe.execute()
    with anyio.fail_after(20):
        while True:
            entries = await w.redis.xrange(event_stream_key(w.conversation))
            mine = [(e["type"], json.loads(e["data"])) for _, e in entries if e["task_id"] == str(task.task_id)]
            if any(t == "done" for t, _ in mine):
                return mine
            await anyio.sleep(0.1)


async def test_answer_then_unresolved_feedback_files_one_ticket_across_a_restart(worker):
    events = await send(worker, "VPN 连不上")
    types = [t for t, _ in events]
    assert types[0] == "retrieving" and types[-2:] == ["answer", "done"]
    assert events[-1][1] == {"outcome": "awaiting_feedback"}

    await worker.restart()  # the interrupted thread must survive in Postgres, not in memory
    events = await send(worker, "按步骤试了，还是不行")
    assert [t for t, _ in events] == ["ticket_created", "done"]

    [(ticket_id, requester, category, summary)] = _tickets(worker.conversation)
    assert str(ticket_id) == events[0][1]["ticket_id"]
    assert (str(requester), category, summary) == (str(worker.user), "vpn", worker.models.summary)

    results = [fields for _, fields in await worker.redis.xrange(RESULT_STREAM)]
    assert [(r["status"], r["outcome"]) for r in results] == [("done", "awaiting_feedback"), ("done", "ticket_created")]
    assert results[0]["content"] == worker.models.answer and results[0]["ticket_id"] == ""
    assert results[1]["ticket_id"] == str(ticket_id) and results[1]["tenant_id"] == str(TENANT)
    await nothing_pending(worker)


async def test_crash_after_the_ticket_was_created_does_not_file_a_second_one(worker):
    worker.models.intent = "ticket_request"
    worker.tools.crash_after_create = 1

    events = await send(worker, "账号被锁了，直接帮我提工单")
    types = [t for t, _ in events]
    assert "resumed" in types and types[-2:] == ["ticket_created", "done"]
    [(ticket_id, *_)] = _tickets(worker.conversation)
    assert str(ticket_id) == events[-2][1]["ticket_id"]


async def _drained(redis) -> bool:
    [group] = await redis.xinfo_groups(TASK_STREAM)
    return group["lag"] == 0 and group["pending"] == 0


@pytest.mark.parametrize("floor", ["kept", "lost"], ids=["redis-floor", "checkpoint-seq"])
async def test_stale_copy_of_an_older_turn_is_skipped(worker, floor):
    """A copy of turn 1 (e.g. requeued by app-service while the original was only slow) reaches
    the worker after turn 2 has left the thread waiting for feedback. It must not be taken as that
    feedback. With the floor lost (Redis failover), the checkpointed seq still refuses it."""
    await send(worker, "VPN 连不上")
    first = worker.last_task
    worker.models.resolution = "new_question"
    events = await send(worker, "邮箱无法收发邮件")
    assert events[-1][1] == {"outcome": "awaiting_feedback"}
    events_before = await worker.redis.xlen(event_stream_key(worker.conversation))
    results_before = await worker.redis.xlen(RESULT_STREAM)
    model_calls_before = list(worker.models.calls)

    if floor == "lost":
        await worker.redis.delete(floor_key(worker.conversation))
    await worker.redis.xadd(TASK_STREAM, task_fields(first))
    with anyio.fail_after(10):
        while not await _drained(worker.redis):
            await anyio.sleep(0.1)

    assert await worker.redis.xlen(event_stream_key(worker.conversation)) == events_before
    assert await worker.redis.xlen(RESULT_STREAM) == results_before
    assert worker.models.calls == model_calls_before and _tickets(worker.conversation) == []
    # The thread still waits for feedback on turn 2, which the employee's real reply then gives.
    worker.models.resolution = "unresolved"
    events = await send(worker, "照着做了，还是收不到")
    assert [t for t, _ in events] == ["ticket_created", "done"]
    [(_, _, category, _)] = _tickets(worker.conversation)
    assert category == "email"


async def test_tenant_without_tool_bindings_gets_a_reply_not_a_ticket(worker):
    """The real TOOL_NOT_ALLOWED path: a tenant onboarded without role_tool_bindings must not have
    every question silently turned into a ticket."""
    events = await send(worker, "VPN 连不上", tenant_id=uuid.uuid4())
    assert events == [
        ("retrieving", {}),
        ("reply", {"content": prompts.KNOWLEDGE_UNAVAILABLE_REPLY}),
        ("done", {"outcome": "knowledge_unavailable"}),
    ]
    assert _tickets(worker.conversation) == []


async def test_budget_refusal_is_acknowledged_not_retried(worker):
    worker.models.over_budget = {"classify"}
    events = await send(worker, "VPN 连不上")
    assert events == [
        ("reply", {"content": prompts.BUDGET_EXCEEDED_REPLY}),
        ("done", {"outcome": "budget_exceeded"}),
    ]
    await nothing_pending(worker)
    assert worker.models.calls == ["classify"]  # refused once, not retried


async def nothing_pending(w) -> None:
    """The task is acknowledged just after its done event is published: wait for it."""
    with anyio.fail_after(2):
        while (await w.redis.xpending(TASK_STREAM, CONSUMER_GROUP))["pending"]:
            await anyio.sleep(0.02)


NORTHWIND = uuid.UUID("9b2d6c1a-4e7f-4a3b-8c5d-2f1e0a9b8c7d")  # demo data: an HR/admin knowledge base


async def test_an_hr_tenant_files_tickets_under_its_own_categories(worker):
    """Nothing IT-specific is built in: the category comes from the tenant's knowledge data."""
    events = await send(worker, "年假怎么申请", tenant_id=NORTHWIND)
    assert events[0] == ("retrieving", {}) and events[-1] == ("done", {"outcome": "awaiting_feedback"})

    worker.models.resolution = "unresolved"
    events = await send(worker, "按流程提交了还是没通过", tenant_id=NORTHWIND)
    assert [t for t, _ in events] == ["ticket_created", "done"]
    [(_, _, category, _)] = _tickets(worker.conversation)
    assert category == "leave"



async def send_decision(w: Worker, approval_id: str, decision: str) -> list[tuple[str, dict]]:
    """What app-service enqueues when tool-gateway announces the decision (an approval turn)."""
    w.seq += 1
    task = make_task(w.conversation, "", user_id=w.user, seq=w.seq, kind="approval", approval_id=approval_id,
                     decision=decision)
    w.last_task = task
    await w.redis.xadd(TASK_STREAM, task_fields(task))
    with anyio.fail_after(20):
        while True:
            entries = await w.redis.xrange(event_stream_key(w.conversation))
            mine = [(e["type"], json.loads(e["data"])) for _, e in entries if e["task_id"] == str(task.task_id)]
            if any(t == "done" for t, _ in mine):
                return mine
            await anyio.sleep(0.1)


def _grants(conversation) -> int:
    with psycopg.connect(GATEWAY_ADMIN_DB_URL) as db:
        return db.execute(
            "SELECT count(*) FROM access_grants WHERE idempotency_key LIKE %s", (f"{conversation}:%",)
        ).fetchone()[0]


async def test_an_action_waits_for_a_handler_and_runs_once_approved(worker):
    """demo's policy makes grant_access wait for approval: the run pauses, a handler approves in
    tool-gateway, and the approval task resumes the run, which grants access exactly once."""
    worker.models.intent = "action"
    events = await send(worker, "帮我开通财务系统的只读权限，月底对账要用")
    assert events[-2:] == [("reply", {"content": prompts.ACTION_SUBMITTED_REPLY}),
                           ("done", {"outcome": "awaiting_approval"})]
    assert _grants(worker.conversation) == 0

    with psycopg.connect(GATEWAY_ADMIN_DB_URL) as db:
        [(approval_id, arguments)] = db.execute(
            "SELECT id::text, arguments FROM tool_approvals WHERE session_id = %s AND status = 'pending'",
            (str(worker.conversation),),
        ).fetchall()
    assert arguments == worker.models.plan["arguments"]
    handler = httpx.post(
        f"{TOOL_GATEWAY_URL}/v1/approvals/{approval_id}/decision",
        json={"tenant_id": str(TENANT), "decision": "approve", "approver_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {TOOL_GATEWAY_TOKEN}"},
    )
    assert handler.json()["status"] == "approved"

    events = await send_decision(worker, approval_id, "approved")
    assert events == [("reply", {"content": prompts.ACTION_APPROVED_REPLY}), ("done", {"outcome": "action_done"})]
    assert _grants(worker.conversation) == 1
