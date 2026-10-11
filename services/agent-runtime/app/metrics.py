"""agent-runtime's metrics (OTel metrics API; exported over OTLP by the OTel agent, like traces).

Labels are low-cardinality on purpose: tenant, outcome, node, intent. Never task, conversation or
user ids, which belong on spans and logs. Bucket boundaries are given explicitly (advisory), since
the SDK's defaults are meant for milliseconds.
"""

import logging
import time
from collections.abc import Iterable

import anyio
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from app.worker.queue import CONSUMER_GROUP, KNOWN_TENANTS, TASK_STREAM, leases_key, task_stream

meter = metrics.get_meter("agent_runtime")
logger = logging.getLogger("agent_runtime.metrics")

TASK_DURATION = meter.create_histogram(
    "app.task.duration", unit="s", description="Handling one delivery of an agent task, by what came of it",
    explicit_bucket_boundaries_advisory=[0.1, 0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90],
)
SHUTDOWN_DRAIN = meter.create_histogram(
    "app.worker.shutdown.drain_duration", unit="s",
    description="From a stop signal until the tasks in flight finished or were handed back",
    explicit_bucket_boundaries_advisory=[0.1, 0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90],
)
TASK_DELIVERIES = meter.create_counter(
    "app.task.deliveries", description="Deliveries of agent tasks, by what came of them",
)
QUEUE_WAIT = meter.create_histogram(
    "app.queue.wait", unit="s", description="Time from enqueue to this delivery (includes earlier attempts)",
    explicit_bucket_boundaries_advisory=[0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 120, 300, 600],
)
NODE_DURATION = meter.create_histogram(
    "app.node.duration", unit="s", description="One run of a graph node",
    explicit_bucket_boundaries_advisory=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30],
)
CLASSIFIED_INTENTS = meter.create_counter("app.agent.intents", description="Classified intents of employee messages")
KNOWLEDGE_SEARCHES = meter.create_counter(
    "app.knowledge.searches", description="Knowledge searches, by result: hit, miss, unavailable",
)


class QueueMonitor:
    """Samples the Redis Streams every interval for gauges an alert can watch: backlog not yet
    delivered (lag), delivered but unacknowledged (pending), how long the oldest pending one has
    waited, and dead letters. The task queue is a stream per tenant, so its gauges carry the tenant
    (sum them for the whole queue), along with how many slots of its concurrency limit each tenant
    holds. Every replica reports the same values; query them with max()."""

    def __init__(self, redis: Redis, streams: dict[str, str], dead_letter_stream: str, interval_s: float = 15):
        self._redis = redis
        self._streams = streams  # stream -> consumer group
        self._dead_letters = dead_letter_stream
        self._interval_s = interval_s
        self.latest: dict[str, dict[str, float]] = {}
        self.tenants: dict[str, dict[str, float]] = {}  # the task queue, by tenant
        meter.create_observable_gauge(
            "app.queue.lag", callbacks=[self._observe("lag")], description="Entries not yet delivered to the group",
        )
        meter.create_observable_gauge(
            "app.queue.pending", callbacks=[self._observe("pending")], description="Delivered, not yet acknowledged",
        )
        meter.create_observable_gauge(
            "app.queue.oldest_pending_age", unit="s", callbacks=[self._observe("oldest_pending_age")],
            description="Age of the oldest unacknowledged entry (0 when none)",
        )
        meter.create_observable_gauge(
            "app.queue.dead_letters", callbacks=[self._observe("dead_letters")], description="Dead-lettered tasks",
        )
        meter.create_observable_gauge(
            "app.queue.tenant_slots", callbacks=[self._observe("slots")],
            description="Tasks a tenant has running, as counted against its concurrency limit",
        )

    def _observe(self, key: str):
        def callback(_: CallbackOptions) -> Iterable[Observation]:
            for stream, values in self.latest.items():
                if key in values:
                    yield Observation(values[key], {"messaging.destination.name": stream})
            for tenant, values in self.tenants.items():
                if key in values:
                    yield Observation(values[key], {"messaging.destination.name": TASK_STREAM, "app.tenant_id": tenant})
        return callback

    async def run(self) -> None:
        while True:
            try:
                await self.sample()
            except (RedisError, OSError):
                logger.warning("sampling queue metrics failed", exc_info=True)
            await anyio.sleep(self._interval_s)

    async def sample(self) -> None:
        for stream, group in self._streams.items():
            if (values := await self._group(stream, group)) is not None:
                self.latest[stream] = values
        self.latest[self._dead_letters] = {"dead_letters": await self._redis.xlen(self._dead_letters)}
        tenants = {}
        now_ms = int(time.time() * 1000)
        for tenant in await self._redis.zrange(KNOWN_TENANTS, 0, -1):
            values = await self._group(task_stream(tenant), CONSUMER_GROUP)
            if values is None:  # no group until the tenant's first task is picked: all of it is backlog
                values = {"lag": await self._redis.xlen(task_stream(tenant)), "pending": 0, "oldest_pending_age": 0.0}
            # Expired leases are only removed by the next pick; don't count them meanwhile.
            values["slots"] = await self._redis.zcount(leases_key(tenant), f"({now_ms}", "+inf")
            tenants[tenant] = values
        self.tenants = tenants

    async def _group(self, stream: str, group: str) -> dict[str, float] | None:
        try:
            groups = await self._redis.xinfo_groups(stream)
        except ResponseError:  # the stream doesn't exist yet
            return None
        info = next((g for g in groups if g["name"] == group), None)
        if info is None:
            return None
        summary = await self._redis.xpending(stream, group)
        oldest = summary.get("min")
        age = max(0.0, time.time() - int(oldest.split("-", 1)[0]) / 1000) if oldest else 0.0
        return {"lag": info.get("lag") or 0, "pending": info["pending"], "oldest_pending_age": age}
