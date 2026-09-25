import json
import uuid

import anyio
import pytest
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from app.core.errors import RetryLater
from app.events import RESULT_STREAM, EventPublisher, event_stream_key
from app.metrics import QueueMonitor
from app.tracing import inject_into
from app.worker.consumer import TaskConsumer
from app.worker.locks import ConversationLock
from app.worker.queue import CONSUMER_GROUP, DEAD_LETTER_STREAM, TASK_STREAM, RedisStreamTaskQueue, floor_key
from tests.conftest import metric_total
from tests.fakes import make_task

pytestmark = pytest.mark.anyio


def queue(redis, consumer="c1", idle_ms=300):
    return RedisStreamTaskQueue(redis, consumer, claim_idle_ms=idle_ms, claim_interval_s=0)


def task_fields(task) -> dict[str, str]:
    """As app-service writes a task: fields a task doesn't use (None) are left out."""
    return {k: str(v) for k, v in task.model_dump().items() if v is not None}


async def pending_count(redis):
    return (await redis.xpending(TASK_STREAM, CONSUMER_GROUP))["pending"]


class FakeRunner:
    def __init__(self, raises=None):
        self.raises = raises
        self.ran: list = []

    async def run(self, task):
        self.ran.append(task.task_id)
        if self.raises:
            raise self.raises
        return "replied"


async def test_acked_task_is_not_redelivered(redis):
    q = queue(redis)
    await q.setup()
    await redis.xadd(TASK_STREAM, task_fields(make_task(uuid.uuid4(), "hi")))
    [delivery] = await q.receive(4, block_ms=100)
    assert delivery.attempt == 1
    await q.ack(delivery)
    await anyio.sleep(0.4)
    assert await queue(redis, "c2").receive(4, block_ms=100) == []
    assert await pending_count(redis) == 0


async def test_abandoned_task_is_reclaimed_by_another_consumer(redis):
    first, second = queue(redis, "c1"), queue(redis, "c2")
    await first.setup()
    msg_id = await redis.xadd(TASK_STREAM, task_fields(make_task(uuid.uuid4(), "hi")))
    await first.receive(4, block_ms=100)  # c1 "crashes" without acknowledging

    assert await second.receive(4, block_ms=100) == []  # not idle long enough yet
    await anyio.sleep(0.4)
    [delivery] = await second.receive(4, block_ms=100)
    assert (delivery.id, delivery.attempt) == (msg_id, 2)


@pytest.mark.parametrize(("raises", "acked"), [(None, True), (RetryLater("busy downstream"), False), (RuntimeError("boom"), False)])
async def test_only_a_finished_run_is_acknowledged(redis, settings, raises, acked):
    q = queue(redis)
    await q.setup()
    await redis.xadd(TASK_STREAM, task_fields(make_task(uuid.uuid4(), "hi")))
    [delivery] = await q.receive(1, block_ms=100)
    runner = FakeRunner(raises)
    consumer = TaskConsumer(q, runner, redis, settings)
    if isinstance(raises, RuntimeError):
        with pytest.raises(RuntimeError):
            await consumer.handle(delivery)
    else:
        await consumer.handle(delivery)
    assert len(runner.ran) == 1
    assert await pending_count(redis) == (0 if acked else 1)


async def test_malformed_task_is_dead_lettered_without_running(redis, settings):
    q = queue(redis)
    await q.setup()
    await redis.xadd(TASK_STREAM, {"task_id": "not-a-uuid", "content": "hi"})
    [delivery] = await q.receive(1, block_ms=100)
    runner = FakeRunner()
    await TaskConsumer(q, runner, redis, settings).handle(delivery)
    assert runner.ran == []
    [(_, dead)] = await redis.xrange(DEAD_LETTER_STREAM)
    assert dead["source_id"] == delivery.id and dead["reason"].startswith("invalid task")
    assert await pending_count(redis) == 0


async def test_task_past_max_deliveries_is_dead_lettered_and_reported(redis, settings):
    q = queue(redis, idle_ms=0)
    await q.setup()
    task = make_task(uuid.uuid4(), "hi")
    await redis.xadd(TASK_STREAM, task_fields(task))
    for _ in range(settings.max_deliveries + 1):
        [delivery] = await q.receive(1, block_ms=100)
    assert delivery.attempt == settings.max_deliveries + 1

    runner = FakeRunner()
    await TaskConsumer(q, runner, redis, settings).handle(delivery)
    assert runner.ran == []
    assert len(await redis.xrange(DEAD_LETTER_STREAM)) == 1
    [(_, event)] = await redis.xrange(event_stream_key(task.conversation_id))
    assert event["type"] == "error" and json.loads(event["data"])["code"] == "TASK_FAILED"
    [(_, result)] = await redis.xrange(RESULT_STREAM)
    assert (result["task_id"], result["status"], result["error_code"]) == (str(task.task_id), "failed", "TASK_FAILED")


async def test_busy_conversation_leaves_the_task_pending(redis, settings):
    q = queue(redis)
    await q.setup()
    task = make_task(uuid.uuid4(), "hi")
    await redis.xadd(TASK_STREAM, task_fields(task))
    [delivery] = await q.receive(1, block_ms=100)
    held = ConversationLock(redis, task.conversation_id, 10)
    assert await held.acquire()

    runner = FakeRunner()
    await TaskConsumer(q, runner, redis, settings).handle(delivery)
    assert runner.ran == [] and await pending_count(redis) == 1


@pytest.mark.parametrize(("seq", "runs"), [(1, False), (2, True)])
async def test_task_below_the_conversation_floor_is_acked_without_running(redis, settings, seq, runs):
    q = queue(redis)
    await q.setup()
    task = make_task(uuid.uuid4(), "hi", seq=seq)
    await redis.set(floor_key(task.conversation_id), 2)  # app-service has since enqueued turn 2
    await redis.xadd(TASK_STREAM, task_fields(task))
    [delivery] = await q.receive(1, block_ms=100)

    runner = FakeRunner()
    await TaskConsumer(q, runner, redis, settings).handle(delivery)
    assert runner.ran == ([task.task_id] if runs else [])
    assert await pending_count(redis) == 0
    assert await redis.xlen(event_stream_key(task.conversation_id)) == 0  # nothing reported for the stale copy
    assert await redis.xlen(RESULT_STREAM) == 0


async def test_a_task_continues_the_trace_it_was_sent_from(redis, settings, spans):
    q = queue(redis)
    await q.setup()
    task = make_task(uuid.uuid4(), "hi")
    with trace.get_tracer("test").start_as_current_span("send agent_tasks") as producer:
        fields = inject_into(task_fields(task))  # what app-service does when it enqueues
    await redis.xadd(TASK_STREAM, fields)
    [delivery] = await q.receive(1, block_ms=100)

    await TaskConsumer(q, FakeRunner(), redis, settings).handle(delivery)
    [consumer] = [s for s in spans.get_finished_spans() if s.name == "process agent_tasks"]
    assert consumer.kind == SpanKind.CONSUMER
    assert consumer.context.trace_id == producer.get_span_context().trace_id
    assert consumer.parent.span_id == producer.get_span_context().span_id
    attrs = consumer.attributes
    assert (attrs["app.task_id"], attrs["app.delivery_attempt"], attrs["messaging.message.id"]) == (
        str(task.task_id), 1, delivery.id,
    )
    assert attrs["app.queue_wait_ms"] >= 0


async def test_skipping_a_stale_copy_is_visible_on_the_span(redis, settings, spans):
    q = queue(redis)
    await q.setup()
    task = make_task(uuid.uuid4(), "hi", seq=1)
    await redis.set(floor_key(task.conversation_id), 2)
    await redis.xadd(TASK_STREAM, task_fields(task))
    [delivery] = await q.receive(1, block_ms=100)
    await TaskConsumer(q, FakeRunner(), redis, settings).handle(delivery)
    [consumer] = [s for s in spans.get_finished_spans() if s.name == "process agent_tasks"]
    assert [(e.name, e.attributes["check"]) for e in consumer.events] == [("skipped_stale", "redis floor")]


async def test_a_result_carries_the_trace_of_the_run_that_sent_it(redis):
    task = make_task(uuid.uuid4(), "hi")
    with trace.get_tracer("test").start_as_current_span("process agent_tasks") as run:
        await EventPublisher(redis, task).finish(outcome="replied", content="你好", ticket_id=None)
    [(_, result)] = await redis.xrange(RESULT_STREAM)
    _, trace_id, span_id, _ = result["traceparent"].split("-")
    context = run.get_span_context()
    assert (trace_id, span_id) == (format(context.trace_id, "032x"), format(context.span_id, "016x"))


async def test_each_delivery_is_counted_by_what_came_of_it(redis, settings):
    q = queue(redis)
    await q.setup()
    task = make_task(uuid.uuid4(), "hi")
    await redis.xadd(TASK_STREAM, task_fields(task))
    [delivery] = await q.receive(1, block_ms=100)
    labels = {"app__result": "replied", "app__tenant_id": str(task.tenant_id)}
    before = (metric_total("app.task.deliveries", **labels), metric_total("app.task.duration", **labels))
    await TaskConsumer(q, FakeRunner(), redis, settings).handle(delivery)  # FakeRunner's outcome: replied
    after = (metric_total("app.task.deliveries", **labels), metric_total("app.task.duration", **labels))
    assert (after[0] - before[0], after[1] - before[1]) == (1, 1)


async def test_queue_monitor_reports_backlog_pending_and_dead_letters(redis):
    q = queue(redis)
    await q.setup()
    for content in ("a", "b", "c"):
        await redis.xadd(TASK_STREAM, task_fields(make_task(uuid.uuid4(), content)))
    await q.receive(1, block_ms=100)  # one delivered and left unacknowledged
    await redis.xadd(DEAD_LETTER_STREAM, {"reason": "test"})

    monitor = QueueMonitor(redis, {TASK_STREAM: CONSUMER_GROUP}, dead_letter_stream=DEAD_LETTER_STREAM)
    await monitor.sample()
    tasks = monitor.latest[TASK_STREAM]
    assert (tasks["lag"], tasks["pending"]) == (2, 1)
    assert 0 <= tasks["oldest_pending_age"] < 60
    assert monitor.latest[DEAD_LETTER_STREAM] == {"dead_letters": 1}
