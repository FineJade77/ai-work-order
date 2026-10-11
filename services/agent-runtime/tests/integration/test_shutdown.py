"""Graceful shutdown against Redis: on stop() the consumer takes no more work, lets its runs finish,
hands back the ones it has to cut off (at once, without counting the attempt) and leaves the group."""

import uuid

import anyio
import pytest

from app.worker.consumer import TaskConsumer
from app.worker.queue import CONSUMER_GROUP, HANDED_BACK, RedisStreamTaskQueue, leases_key, task_stream
from tests.conftest import metric_total
from tests.fakes import TENANT, make_task
from tests.integration.test_queue import enqueue, pending_count, task_fields

pytestmark = pytest.mark.anyio


class SlowRunner:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.started: list = []
        self.finished: list = []

    async def run(self, task):
        self.started.append(task.task_id)
        await anyio.sleep(self.seconds)
        self.finished.append(task.task_id)
        return "replied"


def consumer_queue(redis, name="test-consumer", idle_ms=5000):
    return RedisStreamTaskQueue(
        redis, name, claim_idle_ms=idle_ms, max_concurrency=0, lease_ms=60_000, claim_interval_s=0
    )


def a_task():
    return make_task(uuid.uuid4(), "hi")


async def consumers(redis) -> set[str]:
    return {c["name"] for c in await redis.xinfo_consumers(task_stream(TENANT), CONSUMER_GROUP)}


async def pending_entries(redis) -> list[dict]:
    return await redis.xpending_range(task_stream(TENANT), CONSUMER_GROUP, "-", "+", 10)


async def wait_until(predicate, timeout=5.0):
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.02)


async def running(redis, runner, tasks):
    """Enqueues the tasks and waits until they all run."""
    for task in tasks:
        await enqueue(redis, task_fields(task))
    await wait_until(lambda: len(runner.started) == len(tasks))


async def test_stop_lets_the_runs_in_flight_finish_then_leaves_the_group(redis, settings):
    runner = SlowRunner(0.5)
    consumer = TaskConsumer(consumer_queue(redis), runner, redis, settings)
    tasks = [a_task() for _ in range(2)]
    async with anyio.create_task_group() as tg:
        tg.start_soon(consumer.run)
        await anyio.sleep(0.1)  # group created
        await running(redis, runner, tasks)
        await consumer.stop()
    assert sorted(runner.finished) == sorted(t.task_id for t in tasks)
    assert await pending_count(redis) == 0
    assert "test-consumer" not in await consumers(redis)


async def test_a_run_cut_off_is_handed_back_for_another_consumer_to_take_at_once(redis, settings):
    # The run's own timeout comes after intake closes (up to POLL_BLOCK_MS), so the drain cuts it off.
    settings = settings.model_copy(update={
        "shutdown_drain_seconds": 0.2, "task_timeout_seconds": 8, "conversation_lock_seconds": 9,
        "claim_idle_seconds": 10,
    })
    runner = SlowRunner(10)  # outlives the drain
    consumer = TaskConsumer(consumer_queue(redis), runner, redis, settings)
    task = a_task()
    labels = {"app__result": "handed_back", "app__tenant_id": str(task.tenant_id)}
    before = metric_total("app.task.deliveries", **labels)
    async with anyio.create_task_group() as tg:
        tg.start_soon(consumer.run)
        await anyio.sleep(0.1)
        await running(redis, runner, [task])
        await consumer.stop()

    assert runner.finished == []
    assert metric_total("app.task.deliveries", **labels) - before == 1
    [entry] = await pending_entries(redis)
    assert entry["consumer"] == HANDED_BACK
    assert await redis.get(f"lock:conversation:{task.conversation_id}") is None  # released, not left to expire
    assert await redis.zcard(leases_key(TENANT)) == 0  # and so is its slot in the tenant's limit
    assert "test-consumer" not in await consumers(redis)

    # Reclaimable now rather than after claim_idle (5 s here), as the same attempt: not counted.
    [delivery] = await consumer_queue(redis, "c2").receive(1, block_ms=100)
    assert (delivery.fields["task_id"], delivery.attempt) == (str(task.task_id), 1)


async def test_tasks_received_while_stopping_are_handed_back_unstarted(redis, settings):
    runner = SlowRunner(0)
    consumer = TaskConsumer(consumer_queue(redis), runner, redis, settings)
    task = a_task()
    async with anyio.create_task_group() as tg:
        tg.start_soon(consumer.run)
        await anyio.sleep(0.3)  # blocked reading an empty stream
        tg.start_soon(consumer.stop)
        await anyio.sleep(0.1)
        await enqueue(redis, task_fields(task))  # the wait in progress returns for it
    assert runner.started == []
    [entry] = await pending_entries(redis)
    assert entry["consumer"] == HANDED_BACK
    [delivery] = await consumer_queue(redis, "c2").receive(1, block_ms=100)
    assert delivery.attempt == 1
