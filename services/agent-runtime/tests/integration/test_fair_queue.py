"""Fair scheduling across tenants, against Redis: tenants with tasks waiting are served in turn
(by weight), an idle tenant banks no credit, and a tenant runs no more tasks at once than its
limit, whose slots are leases that a dead consumer's simply outlive."""

import time
import uuid

import anyio
import pytest

from app.worker.consumer import TaskConsumer
from app.worker.queue import CONSUMER_GROUP, TENANT_MAX_CONCURRENCY, TENANT_WEIGHTS, leases_key, task_stream
from tests.fakes import make_task
from tests.integration.test_queue import enqueue, queue, task_fields

pytestmark = pytest.mark.anyio


async def backlog(redis, tenant: str, n: int) -> None:
    for i in range(n):
        await enqueue(redis, {"n": str(i)}, tenant=tenant)


def per_tenant(deliveries) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in deliveries:
        counts[d.tenant] = counts.get(d.tenant, 0) + 1
    return counts


async def test_a_long_backlog_does_not_delay_another_tenant(redis):
    q = queue(redis)
    await q.setup()
    await backlog(redis, "a", 50)
    await backlog(redis, "b", 2)  # enqueued last; first in, first out would serve it 51st
    deliveries = await q.receive(4, block_ms=100)
    assert per_tenant(deliveries) == {"a": 2, "b": 2}
    assert [d.fields["n"] for d in deliveries if d.tenant == "a"] == ["0", "1"]  # in order within a tenant


async def test_a_tenant_of_weight_two_is_served_twice_as_often(redis):
    q = queue(redis)
    await q.setup()
    await redis.hset(TENANT_WEIGHTS, "a", 2)
    await backlog(redis, "a", 10)
    await backlog(redis, "b", 10)
    assert per_tenant(await q.receive(6, block_ms=100)) == {"a": 4, "b": 2}


async def test_time_spent_idle_is_not_credit(redis):
    q = queue(redis)
    await q.setup()
    await backlog(redis, "b", 1)
    await q.receive(1, block_ms=100)  # b was served once, long ago
    await backlog(redis, "a", 10)
    assert per_tenant(await q.receive(10, block_ms=100)) == {"a": 10}
    # b comes back: it joins at the current virtual time, not where it left off ten tasks ago,
    # which would put its next ten tasks ahead of everything a sends.
    await backlog(redis, "b", 5)
    await backlog(redis, "a", 5)
    assert per_tenant(await q.receive(4, block_ms=100)) == {"a": 2, "b": 2}


async def test_a_tenant_at_its_limit_waits_while_others_are_served(redis):
    q = queue(redis, max_concurrency=2)
    await q.setup()
    await backlog(redis, "a", 5)
    await backlog(redis, "b", 1)
    first = await q.receive(5, block_ms=100)
    assert per_tenant(first) == {"a": 2, "b": 1}
    assert await q.receive(5, block_ms=100) == []  # a has three more, and no free slot

    done = next(d for d in first if d.tenant == "a")
    await q.ack(done)
    await q.release(done)
    assert per_tenant(await q.receive(5, block_ms=100)) == {"a": 1}


async def test_a_tenants_own_limit_overrides_the_default(redis):
    q = queue(redis, max_concurrency=0)  # no limit by default
    await q.setup()
    await redis.hset(TENANT_MAX_CONCURRENCY, "a", 1)
    await backlog(redis, "a", 3)
    await backlog(redis, "b", 3)
    assert per_tenant(await q.receive(6, block_ms=100)) == {"a": 1, "b": 3}


async def test_a_dead_consumers_slots_come_back_when_their_leases_expire(redis):
    q = queue(redis, max_concurrency=1, lease_ms=300, idle_ms=60_000)
    await q.setup()
    await backlog(redis, "a", 2)
    [held] = await q.receive(2, block_ms=100)  # the consumer dies here: never released
    assert await q.receive(2, block_ms=100) == []
    await anyio.sleep(0.4)
    [delivery] = await queue(redis, "c2", max_concurrency=1, idle_ms=60_000).receive(2, block_ms=100)
    assert delivery.id != held.id  # the next task; the dead one's waits to be reclaimed


async def test_an_idle_consumer_is_woken_by_an_enqueue(redis):
    q = queue(redis)
    await q.setup()
    received: list = []

    async def wait_for_work():
        received.extend(await q.receive(1, block_ms=5000))

    async with anyio.create_task_group() as tg:
        tg.start_soon(wait_for_work)
        await anyio.sleep(0.2)
        started = time.monotonic()
        await backlog(redis, "a", 1)
    assert len(received) == 1 and time.monotonic() - started < 1


async def test_an_idle_consumer_is_woken_when_a_slot_is_freed(redis):
    first, second = queue(redis, "c1", max_concurrency=1), queue(redis, "c2", max_concurrency=1)
    await first.setup()
    await second.setup()
    await backlog(redis, "a", 2)
    [held] = await first.receive(1, block_ms=100)
    received: list = []

    async def wait_for_work():
        while not received:  # as the consumer loop does: a wake-up may be for work someone else took
            received.extend(await second.receive(1, block_ms=5000))

    async with anyio.create_task_group() as tg:
        tg.start_soon(wait_for_work)
        await anyio.sleep(0.2)
        assert received == []  # a's other task waits for the slot
        started = time.monotonic()
        await first.ack(held)
        await first.release(held)
    assert len(received) == 1 and time.monotonic() - started < 1


async def test_abandoned_tasks_of_every_tenant_are_reclaimed_and_hold_a_slot(redis):
    first, second = queue(redis, "c1", max_concurrency=1), queue(redis, "c2", max_concurrency=1)
    await first.setup()
    await backlog(redis, "a", 1)
    await backlog(redis, "b", 1)
    assert len(await first.receive(2, block_ms=100)) == 2  # c1 "crashes" without acknowledging
    await redis.delete(leases_key("a"), leases_key("b"))  # as if their leases had expired meanwhile
    await anyio.sleep(0.4)
    deliveries = await second.receive(2, block_ms=100)
    assert sorted((d.tenant, d.attempt) for d in deliveries) == [("a", 2), ("b", 2)]
    assert [await redis.zcard(leases_key(t)) for t in ("a", "b")] == [1, 1]


async def test_leaving_removes_the_consumer_from_every_tenants_group(redis):
    q = queue(redis, "leaver")
    await q.setup()
    await backlog(redis, "a", 1)
    await backlog(redis, "b", 1)
    held, other = sorted(await q.receive(2, block_ms=100), key=lambda d: d.tenant)
    await q.ack(other)
    assert await q.leave() is False  # still owns a's delivery, so it stays in a's group for the reclaim

    async def names(tenant):
        return {c["name"] for c in await redis.xinfo_consumers(task_stream(tenant), CONSUMER_GROUP)}

    assert (await names("a"), await names("b")) == ({"leaver"}, set())
    await q.ack(held)
    assert await q.leave() is True and await names("a") == set()


class CountingRunner:
    def __init__(self):
        self.running = self.most_at_once = self.finished = 0

    async def run(self, task):
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        await anyio.sleep(0.1)
        self.running -= 1
        self.finished += 1
        return "replied"


async def test_the_consumer_gives_each_slot_back_when_its_run_ends(redis, settings):
    runner = CountingRunner()
    consumer = TaskConsumer(queue(redis, "test-consumer", max_concurrency=2), runner, redis, settings)
    tenant = uuid.uuid4()
    async with anyio.create_task_group() as tg:
        tg.start_soon(consumer.run)
        await anyio.sleep(0.1)
        for _ in range(6):
            await enqueue(redis, task_fields(make_task(uuid.uuid4(), "hi", tenant_id=tenant)))
        with anyio.fail_after(5):
            while runner.finished < 6:
                await anyio.sleep(0.02)
        await consumer.stop()
    assert runner.most_at_once == 2  # worker_concurrency is 4: the tenant's limit is what held it to 2
    assert await redis.zcard(leases_key(tenant)) == 0
