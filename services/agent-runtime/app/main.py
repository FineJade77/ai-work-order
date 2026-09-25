import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, Response
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from app.clients.ai_gateway import AIGatewayClient
from app.clients.tool_gateway import ToolGatewayClient
from app.core.config import get_settings
from app.core.telemetry import configure_logging, instrument_app
from app.events import RESULT_GROUP, RESULT_STREAM, EventPublisher
from app.metrics import QueueMonitor
from app.graph.builder import build_graph
from app.worker.consumer import POLL_BLOCK_MS, TaskConsumer
from app.worker.queue import CONSUMER_GROUP, DEAD_LETTER_STREAM, TASK_STREAM, RedisStreamTaskQueue
from app.worker.runner import TaskRunner

logger = logging.getLogger("agent_runtime")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    async with AsyncExitStack() as stack:
        # redis-py's default socket timeout (5s in 8.x) would cut off the blocking XREADGROUP poll.
        redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=POLL_BLOCK_MS / 1000 + 5)
        stack.push_async_callback(redis.aclose)
        # Connection settings AsyncPostgresSaver requires.
        pool = AsyncConnectionPool(
            settings.checkpoint_database_url,
            max_size=settings.worker_concurrency + 2,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            open=False,
        )
        await pool.open(wait=True)  # fail at startup, not on the first task, if the DB is unreachable
        stack.push_async_callback(pool.close)
        tool_http = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=settings.tool_gateway_url,
                headers={"Authorization": f"Bearer {settings.tool_gateway_service_token}"},
                timeout=10,
            )
        )
        ai_http = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=settings.ai_gateway_url,
                headers={"Authorization": f"Bearer {settings.ai_gateway_service_token}"},
                timeout=httpx.Timeout(10, read=60),  # gaps between streamed tokens
            )
        )

        runner = TaskRunner(
            build_graph(AsyncPostgresSaver(pool)),
            ToolGatewayClient(tool_http, settings.agent_role),
            AIGatewayClient(ai_http),
            lambda task: EventPublisher(redis, task),
        )
        queue = RedisStreamTaskQueue(
            redis, settings.consumer_name, claim_idle_ms=int(settings.claim_idle_seconds * 1000)
        )
        worker = TaskConsumer(queue, runner, redis, settings)
        consumer = asyncio.create_task(worker.run())
        consumer.add_done_callback(_log_consumer_exit)
        app.state.worker, app.state.consumer = worker, consumer
        monitor = QueueMonitor(
            redis, {TASK_STREAM: CONSUMER_GROUP, RESULT_STREAM: RESULT_GROUP}, dead_letter_stream=DEAD_LETTER_STREAM
        )
        sampler = asyncio.create_task(monitor.run())
        try:
            yield
        finally:
            # uvicorn runs this on SIGTERM: drain (see TaskConsumer.stop), then close the clients.
            await worker.stop()
            sampler.cancel()
            await asyncio.gather(consumer, sampler, return_exceptions=True)


def _log_consumer_exit(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        logger.error("task consumer stopped", exc_info=task.exception())


def create_app() -> FastAPI:
    app = FastAPI(title="agent-runtime", lifespan=lifespan)

    # A worker takes no traffic, so readiness only reports state (kubectl, rollout status); liveness
    # is what restarts it. A stalled event loop fails both: the probe itself times out.
    @app.get("/livez")
    async def livez(response: Response) -> dict[str, str]:
        """Alive unless the consumer died on its own; draining after SIGTERM is alive (a failing
        probe then would get it killed mid-drain)."""
        worker: TaskConsumer | None = getattr(app.state, "worker", None)
        consumer: asyncio.Task | None = getattr(app.state, "consumer", None)
        if consumer is not None and consumer.done() and not worker.stopping:
            response.status_code = 503
            return {"status": "consumer stopped"}
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        worker: TaskConsumer | None = getattr(app.state, "worker", None)
        consumer: asyncio.Task | None = getattr(app.state, "consumer", None)
        if consumer is None or consumer.done():
            response.status_code = 503
            return {"status": "consumer stopped"}
        if worker.stopping:
            response.status_code = 503
            return {"status": "draining"}
        return {"status": "ok"}

    instrument_app(app)
    return app


configure_logging("agent-runtime")
app = create_app()
