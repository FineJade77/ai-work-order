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
    waited, and dead letters. Every replica reports the same values; query them with max()."""

    def __init__(self, redis: Redis, streams: dict[str, str], dead_letter_stream: str, interval_s: float = 15):
        self._redis = redis
        self._streams = streams  # stream -> consumer group
        self._dead_letters = dead_letter_stream
        self._interval_s = interval_s
        self.latest: dict[str, dict[str, float]] = {}
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

    def _observe(self, key: str):
        def callback(_: CallbackOptions) -> Iterable[Observation]:
            for stream, values in self.latest.items():
                if key in values:
                    yield Observation(values[key], {"messaging.destination.name": stream})
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
            try:
                groups = await self._redis.xinfo_groups(stream)
            except ResponseError:  # the stream doesn't exist yet
                continue
            info = next((g for g in groups if g["name"] == group), None)
            if info is None:
                continue
            summary = await self._redis.xpending(stream, group)
            oldest = summary.get("min")
            age = max(0.0, time.time() - int(oldest.split("-", 1)[0]) / 1000) if oldest else 0.0
            self.latest[stream] = {"lag": info.get("lag") or 0, "pending": info["pending"], "oldest_pending_age": age}
        self.latest[self._dead_letters] = {"dead_letters": await self._redis.xlen(self._dead_letters)}
