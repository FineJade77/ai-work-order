import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import ResponseError

TASK_STREAM = "agent_tasks"
CONSUMER_GROUP = "agent_runtime"
DEAD_LETTER_STREAM = "agent_tasks:dead"
# Owner of handed-back deliveries (see hand_back) until some consumer reclaims them. Not a real
# consumer, so the one handing back can leave the group right away.
HANDED_BACK = "handed-back"


def floor_key(conversation_id: object) -> str:
    """Set by app-service, in the same transaction as each enqueue, to that task's seq (only ever
    raised): the conversation's newest turn. A task below it belongs to a turn app-service has
    already closed, so it must not run."""
    return f"agent:floor:{conversation_id}"


@dataclass(frozen=True)
class Delivery:
    id: str
    fields: dict[str, str]
    attempt: int  # 1 on first delivery


class TaskQueue(ABC):
    """At-least-once task delivery. Kept behind an interface so the broker can change
    (e.g. to RabbitMQ) without touching the graph or the consumer loop."""

    @abstractmethod
    async def setup(self) -> None: ...

    @abstractmethod
    async def receive(self, count: int, block_ms: int) -> list[Delivery]:
        """Deliveries abandoned by crashed consumers first, then new ones."""

    @abstractmethod
    async def ack(self, delivery: Delivery) -> None: ...

    @abstractmethod
    async def dead_letter(self, delivery: Delivery, reason: str) -> None: ...

    @abstractmethod
    async def hand_back(self, delivery: Delivery) -> None:
        """Gives up a delivery this consumer will not finish (it is shutting down), for another
        consumer to take now rather than after it sits idle; the cut-off attempt is not counted."""

    @abstractmethod
    async def leave(self) -> bool:
        """Leaves the consumer group if nothing is pending on this consumer. True if it left."""


class RedisStreamTaskQueue(TaskQueue):
    """Consumer group on the agent_tasks stream. A delivery that is never acknowledged stays
    in the group's pending list; once idle for claim_idle_ms any consumer may reclaim it."""

    def __init__(self, redis: Redis, consumer: str, *, claim_idle_ms: int, claim_interval_s: float = 10):
        self._redis = redis
        self._consumer = consumer
        self._claim_idle_ms = claim_idle_ms
        self._claim_interval_s = claim_interval_s
        self._next_claim_at = 0.0
        self._claim_cursor = "0-0"

    async def setup(self) -> None:
        try:
            await self._redis.xgroup_create(TASK_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def receive(self, count: int, block_ms: int) -> list[Delivery]:
        deliveries = await self._reclaim(count) if time.monotonic() >= self._next_claim_at else []
        if len(deliveries) < count:
            response = await self._redis.xreadgroup(
                CONSUMER_GROUP,
                self._consumer,
                {TASK_STREAM: ">"},
                count=count - len(deliveries),
                block=None if deliveries else block_ms,
            )
            for _, messages in _stream_entries(response):
                deliveries += [Delivery(id=msg_id, fields=fields, attempt=1) for msg_id, fields in messages]
        return deliveries

    async def _reclaim(self, count: int) -> list[Delivery]:
        next_cursor, messages, *_ = await self._redis.xautoclaim(
            TASK_STREAM, CONSUMER_GROUP, self._consumer, self._claim_idle_ms, start_id=self._claim_cursor, count=count
        )
        # A cursor of 0-0 means the scan wrapped; wait an interval before scanning again.
        self._claim_cursor = next_cursor
        if next_cursor == "0-0":
            self._next_claim_at = time.monotonic() + self._claim_interval_s
        deliveries = []
        for msg_id, fields in messages:
            pending = await self._redis.xpending_range(TASK_STREAM, CONSUMER_GROUP, min=msg_id, max=msg_id, count=1)
            attempt = pending[0]["times_delivered"] if pending else 1
            deliveries.append(Delivery(id=msg_id, fields=fields, attempt=attempt))
        return deliveries

    async def ack(self, delivery: Delivery) -> None:
        await self._redis.xack(TASK_STREAM, CONSUMER_GROUP, delivery.id)

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xadd(DEAD_LETTER_STREAM, {**delivery.fields, "source_id": delivery.id, "reason": reason})
            pipe.xack(TASK_STREAM, CONSUMER_GROUP, delivery.id)
            await pipe.execute()

    async def hand_back(self, delivery: Delivery) -> None:
        # IDLE makes it reclaimable at once (every consumer's next XAUTOCLAIM, within
        # claim_interval_s). XAUTOCLAIM counts a delivery, so RETRYCOUNT is set one lower than this
        # attempt: the next consumer sees the same attempt number. JUSTID: this claim counts nothing.
        await self._redis.xclaim(
            TASK_STREAM, CONSUMER_GROUP, HANDED_BACK, 0, [delivery.id],
            idle=self._claim_idle_ms, retrycount=delivery.attempt - 1, justid=True,
        )

    async def leave(self) -> bool:
        # A deleted consumer's pending entries could no longer be reclaimed, so only leave when
        # there are none (a delivery lost in a failed hand-back waits for the claim instead).
        if await self._redis.xpending_range(TASK_STREAM, CONSUMER_GROUP, "-", "+", 1, consumername=self._consumer):
            return False
        await self._redis.xgroup_delconsumer(TASK_STREAM, CONSUMER_GROUP, self._consumer)
        return True


def _stream_entries(response) -> list:
    # RESP2 returns [[stream, messages]], RESP3 returns {stream: messages}.
    if not response:
        return []
    return list(response.items()) if isinstance(response, dict) else response
