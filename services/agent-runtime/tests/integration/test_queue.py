import json
import uuid

import anyio
import pytest

from app.core.errors import RetryLater
from app.events import RESULT_STREAM, event_stream_key
from app.worker.consumer import TaskConsumer
from app.worker.locks import ConversationLock
from app.worker.queue import CONSUMER_GROUP, DEAD_LETTER_STREAM, TASK_STREAM, RedisStreamTaskQueue
from tests.fakes import make_task

pytestmark = pytest.mark.anyio


def queue(redis, consumer="c1", idle_ms=300):
    return RedisStreamTaskQueue(redis, consumer, claim_idle_ms=idle_ms, claim_interval_s=0)


def task_fields(task):
    return {k: str(v) for k, v in task.model_dump().items()}


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
