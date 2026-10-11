import logging
import time

import anyio
from anyio.abc import TaskGroup
from opentelemetry.trace import SpanKind
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.errors import RetryLater
from app.events import EventPublisher
from app.metrics import QUEUE_WAIT, SHUTDOWN_DRAIN, TASK_DELIVERIES, TASK_DURATION
from app.schemas.tasks import AgentTask
from app.tracing import context_of, event, messaging_attributes, queue_wait_ms, tracer
from app.worker.locks import ConversationLock
from app.worker.queue import CONSUMER_GROUP, TASK_STREAM, Delivery, TaskQueue, floor_key
from app.worker.runner import TaskRunner

logger = logging.getLogger("agent_runtime.consumer")

POLL_BLOCK_MS = 5000
FAILED_MESSAGE = "抱歉，这条消息处理失败了。请稍后重试；如果问题紧急，回复「提工单」转人工处理。"


class TaskConsumer:
    """Pulls tasks and runs up to worker_concurrency of them at once.

    A task is acknowledged only after its run finished and published 'done', or once it is known
    to be a stale copy of an older turn (then nothing is published). Anything else
    (crash, timeout, error, RetryLater) leaves it pending, to be reclaimed by some consumer
    after claim_idle_seconds and resumed from its last checkpoint.

    Which task comes next is the queue's business: it serves tenants in turn and caps how many
    tasks each has running. The slot a delivery holds in that cap is given back when this consumer
    is done with it, whatever came of it.

    stop() shuts down gracefully (SIGTERM: a rolling deploy, a scale-in): take no more work, let
    the runs in flight finish for up to shutdown_drain_seconds, hand back whatever is left so
    another replica takes it at once, and leave the consumer group.
    """

    def __init__(self, queue: TaskQueue, runner: TaskRunner, redis: Redis, settings: Settings):
        self._queue = queue
        self._runner = runner
        self._redis = redis
        self._settings = settings
        self._slots = anyio.Semaphore(settings.worker_concurrency)
        self._intake = anyio.CancelScope()
        self._running: dict[tuple[str, str], anyio.CancelScope] = {}  # (tenant, delivery id) -> its run
        self._unstarted: list[Delivery] = []
        self.stopping = False
        self._drain_deadline = 0.0
        self._stopped = anyio.Event()

    async def run(self) -> None:
        try:
            await self._queue.setup()
            async with anyio.create_task_group() as tg:
                with self._intake:
                    await self._take_work(tg)
                await self._drain()
            await self._leave()
        finally:
            self._stopped.set()

    async def stop(self) -> None:
        """Returns once the consumer has drained and left (it may already have stopped)."""
        self.stopping = True
        # Counted from the signal: intake may take up to POLL_BLOCK_MS more to close.
        self._drain_deadline = anyio.current_time() + self._settings.shutdown_drain_seconds
        self._intake.cancel()
        await self._stopped.wait()

    async def _take_work(self, tg: TaskGroup) -> None:
        while True:
            await self._slots.acquire()  # wait for a free slot before taking work
            try:
                # Shielded: cancelled mid-read, entries Redis already assigned to this consumer
                # would sit unseen until reclaimed. Returns within POLL_BLOCK_MS.
                with anyio.CancelScope(shield=True):
                    deliveries = await self._queue.receive(1 + self._slots.value, POLL_BLOCK_MS)
            except (RedisError, OSError):
                logger.exception("receiving tasks failed; retrying")
                self._slots.release()
                await anyio.sleep(1)
                continue
            if self._intake.cancel_called:
                self._unstarted = deliveries  # handed back by _drain, outside this cancelled scope
                self._slots.release()
                return
            if not deliveries:
                self._slots.release()
                continue
            for i, delivery in enumerate(deliveries):
                if i:
                    self._slots.acquire_nowait()  # receive() asked for no more than were free
                tg.start_soon(self._process, delivery)

    async def _drain(self) -> None:
        started = time.monotonic()
        in_flight = len(self._running)
        logger.info("stopping: %d task(s) in flight, %d received but not started", in_flight, len(self._unstarted))
        for delivery in self._unstarted:
            await self._hand_back(delivery)
        with anyio.move_on_at(self._drain_deadline):
            while self._running:
                await anyio.sleep(0.1)
        cut_off = list(self._running.values())
        for scope in cut_off:
            scope.cancel()  # each run hands its own delivery back (see _process)
        SHUTDOWN_DRAIN.record(time.monotonic() - started)
        logger.info(
            "drained in %.1fs: %d finished, %d cut off and handed back",
            time.monotonic() - started, in_flight - len(cut_off), len(cut_off) + len(self._unstarted),
        )

    async def _leave(self) -> None:
        try:
            with anyio.CancelScope(shield=True):
                left = await self._queue.leave()
            if not left:
                logger.warning("tasks still pending on this consumer; staying in the group for them to be reclaimed")
        except (RedisError, OSError):
            logger.warning("could not leave the consumer group", exc_info=True)

    async def _hand_back(self, delivery: Delivery) -> None:
        try:
            with anyio.CancelScope(shield=True):
                await self._queue.hand_back(delivery)
        except (RedisError, OSError):
            logger.warning("handing back %s failed; it is reclaimed once idle", delivery.id, exc_info=True)

    async def _release(self, delivery: Delivery) -> None:
        try:
            with anyio.CancelScope(shield=True):
                await self._queue.release(delivery)
        except (RedisError, OSError):
            logger.warning("releasing %s's tenant slot failed; it frees itself when the lease expires", delivery.id, exc_info=True)

    async def _process(self, delivery: Delivery) -> None:
        key = (delivery.tenant, delivery.id)
        with anyio.CancelScope() as scope:
            self._running[key] = scope
            try:
                await self.handle(delivery)
            except Exception:
                logger.exception("task delivery %s failed; it stays pending for redelivery", delivery.id)
        del self._running[key]
        if scope.cancelled_caught:  # cut off by stop()
            await self._hand_back(delivery)
        else:
            await self._release(delivery)
        self._slots.release()

    async def handle(self, delivery: Delivery) -> None:
        """One consumer span per delivery, a child of the span app-service sent the task from, so
        every attempt (redeliveries included) shows up in the turn's trace."""
        fields = delivery.fields
        attributes = messaging_attributes(
            TASK_STREAM, "process", task_id=fields.get("task_id"), seq=fields.get("seq"),
            conversation_id=fields.get("conversation_id"), tenant_id=fields.get("tenant_id"),
        )
        attributes |= {
            "messaging.message.id": delivery.id,
            "messaging.consumer.group.name": CONSUMER_GROUP,
            "app.delivery_attempt": delivery.attempt,
            "app.queue_wait_ms": queue_wait_ms(delivery.id),
        }
        QUEUE_WAIT.record(attributes["app.queue_wait_ms"] / 1000, {"app.tenant_id": fields.get("tenant_id", "")})
        started = time.monotonic()
        result = "error"
        with tracer.start_as_current_span(
            f"process {TASK_STREAM}", context=context_of(fields), kind=SpanKind.CONSUMER, attributes=attributes
        ) as span:
            try:
                result = await self._handle(delivery)
            except anyio.get_cancelled_exc_class():
                if self.stopping:
                    result = "handed_back"
                    event("handed_back", reason="shutdown")
                raise
            finally:
                span.set_attribute("app.result", result)
                labels = {"app.result": result, "app.tenant_id": fields.get("tenant_id", "")}
                TASK_DELIVERIES.add(1, labels)
                TASK_DURATION.record(time.monotonic() - started, labels)

    async def _handle(self, delivery: Delivery) -> str:
        """Returns what came of the delivery: the run's outcome (awaiting_feedback, ticket_created,
        superseded...) or dead_letter, conversation_busy, skipped_stale, retry_later."""
        try:
            task = AgentTask.model_validate(delivery.fields)
        except ValidationError as exc:
            # Retrying cannot fix a malformed message.
            event("dead_letter", reason="invalid task")
            await self._queue.dead_letter(delivery, f"invalid task: {exc.error_count()} validation errors")
            return "dead_letter"

        if delivery.attempt > self._settings.max_deliveries:
            event("dead_letter", reason="max deliveries", attempts=delivery.attempt - 1)
            await self._queue.dead_letter(delivery, f"gave up after {delivery.attempt - 1} attempts")
            await EventPublisher(self._redis, task).fail(code="TASK_FAILED", message=FAILED_MESSAGE)
            return "dead_letter"

        lock = ConversationLock(self._redis, task.conversation_id, self._settings.conversation_lock_seconds)
        if not await lock.acquire():
            event("conversation_busy")
            logger.info("conversation %s is busy; task_id=%s will be redelivered", task.conversation_id, task.task_id)
            return "conversation_busy"
        try:
            # Checked under the lock, so a newer turn cannot start between this check and the run.
            if await self._below_floor(task):
                event("skipped_stale", check="redis floor")
                logger.warning(
                    "skipping stale task_id=%s seq=%d: conversation %s has a newer turn",
                    task.task_id, task.seq, task.conversation_id,
                )
                await self._queue.ack(delivery)
                return "skipped_stale"
            with anyio.fail_after(self._settings.task_timeout_seconds):
                outcome = await self._runner.run(task)
            await self._queue.ack(delivery)
            return outcome
        except RetryLater as exc:
            event("retry_later", reason=exc)
            logger.info("task_id=%s retry later: %s", task.task_id, exc)
            return "retry_later"
        finally:
            # Shielded: a run cut off at shutdown must still free the conversation for whoever
            # takes the task over, instead of leaving it locked until the lock expires.
            with anyio.CancelScope(shield=True):
                await lock.release()

    async def _below_floor(self, task: AgentTask) -> bool:
        # No floor (expired, or lost with Redis) means no verdict here; the runner's checkpointed
        # seq still refuses anything older than what the thread has already processed.
        floor = await self._redis.get(floor_key(task.conversation_id))
        return floor is not None and task.seq < int(floor)
