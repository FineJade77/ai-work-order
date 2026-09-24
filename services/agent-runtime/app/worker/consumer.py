import logging

import anyio
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.errors import RetryLater
from app.events import EventPublisher
from app.schemas.tasks import AgentTask
from app.worker.locks import ConversationLock
from app.worker.queue import Delivery, TaskQueue, floor_key
from app.worker.runner import TaskRunner

logger = logging.getLogger("agent_runtime.consumer")

POLL_BLOCK_MS = 5000
FAILED_MESSAGE = "抱歉，这条消息处理失败了。请稍后重试，或直接提交 IT 工单。"


class TaskConsumer:
    """Pulls tasks and runs up to worker_concurrency of them at once.

    A task is acknowledged only after its run finished and published 'done', or once it is known
    to be a stale copy of an older turn (then nothing is published). Anything else
    (crash, timeout, error, RetryLater) leaves it pending, to be reclaimed by some consumer
    after claim_idle_seconds and resumed from its last checkpoint.
    """

    def __init__(self, queue: TaskQueue, runner: TaskRunner, redis: Redis, settings: Settings):
        self._queue = queue
        self._runner = runner
        self._redis = redis
        self._settings = settings
        self._slots = anyio.Semaphore(settings.worker_concurrency)

    async def run(self) -> None:
        await self._queue.setup()
        async with anyio.create_task_group() as tg:
            while True:
                await self._slots.acquire()  # wait for a free slot before taking work
                try:
                    deliveries = await self._queue.receive(1 + self._slots.value, POLL_BLOCK_MS)
                except (RedisError, OSError):
                    logger.exception("receiving tasks failed; retrying")
                    self._slots.release()
                    await anyio.sleep(1)
                    continue
                if not deliveries:
                    self._slots.release()
                    continue
                for i, delivery in enumerate(deliveries):
                    if i:
                        self._slots.acquire_nowait()  # receive() asked for no more than were free
                    tg.start_soon(self._process, delivery)

    async def _process(self, delivery: Delivery) -> None:
        try:
            await self.handle(delivery)
        except Exception:
            logger.exception("task delivery %s failed; it stays pending for redelivery", delivery.id)
        finally:
            self._slots.release()

    async def handle(self, delivery: Delivery) -> None:
        try:
            task = AgentTask.model_validate(delivery.fields)
        except ValidationError as exc:
            # Retrying cannot fix a malformed message.
            await self._queue.dead_letter(delivery, f"invalid task: {exc.error_count()} validation errors")
            return

        if delivery.attempt > self._settings.max_deliveries:
            await self._queue.dead_letter(delivery, f"gave up after {delivery.attempt - 1} attempts")
            await EventPublisher(self._redis, task).fail(code="TASK_FAILED", message=FAILED_MESSAGE)
            return

        lock = ConversationLock(self._redis, task.conversation_id, self._settings.conversation_lock_seconds)
        if not await lock.acquire():
            logger.info("conversation %s is busy; task_id=%s will be redelivered", task.conversation_id, task.task_id)
            return
        try:
            # Checked under the lock, so a newer turn cannot start between this check and the run.
            if await self._below_floor(task):
                logger.warning(
                    "skipping stale task_id=%s seq=%d: conversation %s has a newer turn",
                    task.task_id, task.seq, task.conversation_id,
                )
                await self._queue.ack(delivery)
                return
            with anyio.fail_after(self._settings.task_timeout_seconds):
                await self._runner.run(task)
            await self._queue.ack(delivery)
        except RetryLater as exc:
            logger.info("task_id=%s retry later: %s", task.task_id, exc)
        finally:
            await lock.release()

    async def _below_floor(self, task: AgentTask) -> bool:
        # No floor (expired, or lost with Redis) means no verdict here; the runner's checkpointed
        # seq still refuses anything older than what the thread has already processed.
        floor = await self._redis.get(floor_key(task.conversation_id))
        return floor is not None and task.seq < int(floor)
