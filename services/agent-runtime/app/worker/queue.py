import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import ResponseError

# The queue's name in spans and metrics. The tasks themselves are in one stream per tenant
# (task_stream), so that tenants can be served in turn; see RedisStreamTaskQueue.
TASK_STREAM = "agent_tasks"
CONSUMER_GROUP = "agent_runtime"
DEAD_LETTER_STREAM = "agent_tasks:dead"
# Owner of handed-back deliveries (see hand_back) until some consumer reclaims them. Not a real
# consumer, so the one handing back can leave the group right away.
HANDED_BACK = "handed-back"

# Scheduling state, shared with app-service's enqueue script (services share no code: the names
# are repeated in app-service/app/clients/agent_bus.py).
ACTIVE_TENANTS = "agent_sched:active"  # ZSET: tenants with undelivered tasks -> virtual start time of the next
KNOWN_TENANTS = "agent_sched:tenants"  # ZSET: every tenant seen -> virtual time it has been served up to
VIRTUAL_TIME = "agent_sched:vtime"
WAKE_STREAM = "agent_sched:wake"  # an entry per enqueue or freed slot; idle consumers block on it
# Per-tenant overrides, set by hand (HSET <key> <tenant id> <value>); they apply from the next pick.
TENANT_WEIGHTS = "agent_sched:weight"  # default 1: a tenant of weight 2 is served twice as often
TENANT_MAX_CONCURRENCY = "agent_sched:max_concurrency"  # default Settings.tenant_max_concurrency; 0 = no limit
_STREAM_PREFIX = "agent_tasks:t:"
_LEASES_PREFIX = "agent_sched:leases:"  # + tenant. ZSET: delivery id -> when its lease expires (ms)


def task_stream(tenant: object) -> str:
    return f"{_STREAM_PREFIX}{tenant}"


def leases_key(tenant: object) -> str:
    return f"{_LEASES_PREFIX}{tenant}"


def floor_key(conversation_id: object) -> str:
    """Set by app-service, in the same transaction as each enqueue, to that task's seq (only ever
    raised): the conversation's newest turn. A task below it belongs to a turn app-service has
    already closed, so it must not run."""
    return f"agent:floor:{conversation_id}"


@dataclass(frozen=True)
class Delivery:
    tenant: str  # whose stream it is in; ids are only unique within a stream
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
    async def release(self, delivery: Delivery) -> None:
        """Frees the slot the delivery held in its tenant's concurrency limit. For a delivery this
        consumer is done with, whatever came of it (hand_back does this itself)."""

    @abstractmethod
    async def leave(self) -> bool:
        """Leaves the consumer group if nothing is pending on this consumer. True if it left."""


# Start-time fair queuing over tenants, plus a cap on how many tasks one tenant has running.
# Takes up to ARGV[3] tasks, each from the tenant with the lowest virtual time that is under its cap.
# Serving a tenant moves its virtual time on by 1/weight, so a tenant with a long backlog and one
# with a single task alternate rather than queue behind each other.
# KEYS: active, known, vtime, weights, max_concurrency
# ARGV: group, consumer, count, default max concurrency, lease ms, stream prefix, leases prefix
# Returns [[tenant, id, [field, value, ...]], ...]. Not for Redis Cluster: it builds key names.
_PICK = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local vtime = tonumber(redis.call('GET', KEYS[3]) or '0')
local picked, skip = {}, 0
while #picked < tonumber(ARGV[3]) do
  local head = redis.call('ZRANGE', KEYS[1], skip, skip, 'WITHSCORES')
  if #head == 0 then break end
  local tenant, tag = head[1], tonumber(head[2])
  local leases = ARGV[7] .. tenant
  redis.call('ZREMRANGEBYSCORE', leases, '-inf', now)  -- leases of consumers that died
  local cap = tonumber(redis.call('HGET', KEYS[5], tenant) or ARGV[4]) or tonumber(ARGV[4])
  if cap > 0 and redis.call('ZCARD', leases) >= cap then
    skip = skip + 1  -- stays active: it is picked again once one of its tasks ends
  else
    local stream = ARGV[6] .. tenant
    local read = redis.pcall('XREADGROUP', 'GROUP', ARGV[1], ARGV[2], 'COUNT', 1, 'STREAMS', stream, '>')
    if type(read) == 'table' and read.err then
      if not string.find(read.err, 'NOGROUP') then return redis.error_reply(read.err) end
      redis.call('XGROUP', 'CREATE', stream, ARGV[1], '0', 'MKSTREAM')  -- a tenant's first task
      read = redis.call('XREADGROUP', 'GROUP', ARGV[1], ARGV[2], 'COUNT', 1, 'STREAMS', stream, '>')
    end
    if not read then
      redis.call('ZREM', KEYS[1], tenant)  -- nothing undelivered; enqueue puts it back
    else
      local entry = read[1][2][1]
      local weight = tonumber(redis.call('HGET', KEYS[4], tenant) or '1')
      if not weight or weight <= 0 then weight = 1 end
      -- max(): a tenant held back by its own cap does not bank that time as credit.
      vtime = math.max(vtime, tag)
      local next = string.format('%.17g', vtime + 1 / weight)
      redis.call('ZADD', KEYS[1], next, tenant)
      redis.call('ZADD', KEYS[2], next, tenant)
      redis.call('ZADD', leases, now + tonumber(ARGV[5]), entry[1])
      picked[#picked + 1] = {tenant, entry[1], entry[2]}
    end
  end
end
redis.call('SET', KEYS[3], string.format('%.17g', vtime))
return picked
"""

# A reclaimed delivery was admitted under the cap once already, so it takes a lease regardless.
# KEYS: leases. ARGV: delivery id, lease ms.
_LEASE = """
local t = redis.call('TIME')
redis.call('ZADD', KEYS[1], t[1] * 1000 + math.floor(t[2] / 1000) + tonumber(ARGV[2]), ARGV[1])
"""

# Wakes idle consumers only if the tenant has tasks waiting for the slot.
# KEYS: leases, active, wake. ARGV: delivery id, tenant.
_RELEASE = """
if redis.call('ZREM', KEYS[1], ARGV[1]) == 1 and redis.call('ZSCORE', KEYS[2], ARGV[2]) then
  redis.call('XADD', KEYS[3], 'MAXLEN', '~', 100, '*', 'tenant', ARGV[2])
end
"""


class RedisStreamTaskQueue(TaskQueue):
    """One stream per tenant, each with the same consumer group, and a fair pick across them
    (_PICK): no tenant's backlog delays another tenant's tasks, and no tenant runs more than
    max_concurrency tasks at once across all replicas.

    A slot in that limit is a lease that expires (lease_ms, longer than any run): a consumer that
    is killed never gives its slots back, they lapse. A delivery that is never acknowledged stays
    in its group's pending list; once idle for claim_idle_ms any consumer may reclaim it.
    """

    def __init__(
        self, redis: Redis, consumer: str, *, claim_idle_ms: int, max_concurrency: int, lease_ms: int,
        claim_interval_s: float = 10,
    ):
        self._redis = redis
        self._consumer = consumer
        self._claim_idle_ms = claim_idle_ms
        self._max_concurrency = max_concurrency
        self._lease_ms = lease_ms
        self._claim_interval_s = claim_interval_s
        self._next_claim_at = 0.0
        self._claim_round: list[str] = []  # tenants still to scan in this round of reclaiming
        self._claim_cursor = "0-0"
        self._wake_cursor = "0-0"

    async def setup(self) -> None:
        # Wake-ups from here on: read before the first pick, so none sent after it is missed.
        latest = await self._redis.xrevrange(WAKE_STREAM, count=1)
        self._wake_cursor = latest[0][0] if latest else "0-0"

    async def receive(self, count: int, block_ms: int) -> list[Delivery]:
        deliveries = await self._reclaim(count) if time.monotonic() >= self._next_claim_at else []
        if len(deliveries) < count:
            deliveries += await self._pick(count - len(deliveries))
        if not deliveries:
            await self._wait_for_wake(block_ms)
            deliveries = await self._pick(count)
        return deliveries

    async def _pick(self, count: int) -> list[Delivery]:
        picked = await self._redis.eval(
            _PICK, 5, ACTIVE_TENANTS, KNOWN_TENANTS, VIRTUAL_TIME, TENANT_WEIGHTS, TENANT_MAX_CONCURRENCY,
            CONSUMER_GROUP, self._consumer, count, self._max_concurrency, self._lease_ms, _STREAM_PREFIX, _LEASES_PREFIX,
        )
        return [
            Delivery(tenant=tenant, id=msg_id, fields=dict(zip(fields[::2], fields[1::2])), attempt=1)
            for tenant, msg_id, fields in picked
        ]

    async def _wait_for_wake(self, block_ms: int) -> None:
        response = await self._redis.xread({WAKE_STREAM: self._wake_cursor}, count=100, block=block_ms)
        for _, messages in _stream_entries(response):
            self._wake_cursor = messages[-1][0]

    async def _reclaim(self, count: int) -> list[Delivery]:
        """One round scans every tenant's pending list once; then wait an interval before the next."""
        if not self._claim_round:
            self._claim_round = await self._redis.zrange(KNOWN_TENANTS, 0, -1)
        deliveries: list[Delivery] = []
        while self._claim_round and len(deliveries) < count:
            tenant = self._claim_round[-1]
            stream = task_stream(tenant)
            try:
                self._claim_cursor, messages, *_ = await self._redis.xautoclaim(
                    stream, CONSUMER_GROUP, self._consumer, self._claim_idle_ms,
                    start_id=self._claim_cursor, count=count - len(deliveries),
                )
            except ResponseError as exc:
                if "NOGROUP" not in str(exc):
                    raise
                self._claim_cursor, messages = "0-0", []  # nothing was ever picked from it
            for msg_id, fields in messages:
                pending = await self._redis.xpending_range(stream, CONSUMER_GROUP, min=msg_id, max=msg_id, count=1)
                attempt = pending[0]["times_delivered"] if pending else 1
                await self._redis.eval(_LEASE, 1, leases_key(tenant), msg_id, self._lease_ms)
                deliveries.append(Delivery(tenant=tenant, id=msg_id, fields=fields, attempt=attempt))
            if self._claim_cursor == "0-0":  # this tenant's scan wrapped
                self._claim_round.pop()
        if not self._claim_round:
            self._next_claim_at = time.monotonic() + self._claim_interval_s
        return deliveries

    async def ack(self, delivery: Delivery) -> None:
        await self._redis.xack(task_stream(delivery.tenant), CONSUMER_GROUP, delivery.id)

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xadd(
                DEAD_LETTER_STREAM,
                {**delivery.fields, "source_tenant": delivery.tenant, "source_id": delivery.id, "reason": reason},
            )
            pipe.xack(task_stream(delivery.tenant), CONSUMER_GROUP, delivery.id)
            await pipe.execute()

    async def hand_back(self, delivery: Delivery) -> None:
        # IDLE makes it reclaimable at once (every consumer's next XAUTOCLAIM, within
        # claim_interval_s). XAUTOCLAIM counts a delivery, so RETRYCOUNT is set one lower than this
        # attempt: the next consumer sees the same attempt number. JUSTID: this claim counts nothing.
        await self._redis.xclaim(
            task_stream(delivery.tenant), CONSUMER_GROUP, HANDED_BACK, 0, [delivery.id],
            idle=self._claim_idle_ms, retrycount=delivery.attempt - 1, justid=True,
        )
        await self.release(delivery)

    async def release(self, delivery: Delivery) -> None:
        await self._redis.eval(
            _RELEASE, 3, leases_key(delivery.tenant), ACTIVE_TENANTS, WAKE_STREAM, delivery.id, delivery.tenant
        )

    async def leave(self) -> bool:
        # The consumer exists in the group of every stream it read or scanned. A deleted consumer's
        # pending entries could no longer be reclaimed, so it stays in the groups where it has any
        # (a delivery lost in a failed hand-back waits for the claim instead).
        left = True
        for tenant in await self._redis.zrange(KNOWN_TENANTS, 0, -1):
            stream = task_stream(tenant)
            try:
                if await self._redis.xpending_range(stream, CONSUMER_GROUP, "-", "+", 1, consumername=self._consumer):
                    left = False
                else:
                    await self._redis.xgroup_delconsumer(stream, CONSUMER_GROUP, self._consumer)
            except ResponseError as exc:
                if "NOGROUP" not in str(exc):
                    raise
        return left


def _stream_entries(response) -> list:
    # RESP2 returns [[stream, messages]], RESP3 returns {stream: messages}.
    if not response:
        return []
    return list(response.items()) if isinstance(response, dict) else response
