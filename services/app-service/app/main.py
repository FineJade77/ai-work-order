import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.api.v1.router import api_router
from app.clients.agent_bus import AgentBus
from app.clients.ticket_service import TicketServiceClient
from app.core.config import get_settings
from app.core.errors import ApiError
from app.services.rate_limit import MessageRateLimiter
from app.services.stream_service import READ_BLOCK_MS
from app.services.ticket_access import TicketAccess
from app.workers.requeuer import Requeuer
from app.workers.result_collector import ResultCollector

logger = logging.getLogger("app_service")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # fail at startup, not on the first request, if config is missing
    async with AsyncExitStack() as stack:
        # Above the longest blocking read (SSE), or redis-py's default timeout would cut it off.
        redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=READ_BLOCK_MS / 1000 + 5)
        stack.push_async_callback(redis.aclose)
        tickets_http = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=settings.ticket_service_url,
                headers={"Authorization": f"Bearer {settings.ticket_service_token}"},
                timeout=10,
            )
        )
        app.state.bus = AgentBus(redis)
        app.state.rate_limiter = MessageRateLimiter(
            redis,
            per_minute=settings.message_rate_per_minute,
            per_day=settings.message_rate_per_day,
            timezone=settings.rate_limit_timezone,
        )
        app.state.ticket_access = TicketAccess(TicketServiceClient(tickets_http))
        app.state.workers = [
            asyncio.create_task(ResultCollector(redis, settings.consumer_name).run(), name="result-collector"),
            asyncio.create_task(Requeuer(app.state.bus, settings).run(), name="requeuer"),
        ]
        for task in app.state.workers:
            task.add_done_callback(_log_worker_exit)
        try:
            yield
        finally:
            for task in app.state.workers:
                task.cancel()
            await asyncio.gather(*app.state.workers, return_exceptions=True)


def _log_worker_exit(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        logger.error("background worker %s stopped", task.get_name(), exc_info=task.exception())


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status, content={"error": {"code": exc.code, "message": exc.message}}, headers=exc.headers
    )


def create_app() -> FastAPI:
    app = FastAPI(title="app-service", lifespan=lifespan)
    app.add_exception_handler(ApiError, api_error_handler)

    @app.get("/healthz")
    async def healthz(response: Response) -> dict[str, str]:
        workers = getattr(app.state, "workers", [])
        if any(task.done() for task in workers):
            response.status_code = 503
            return {"status": "background worker stopped"}
        return {"status": "ok"}

    app.include_router(api_router)
    return app


app = create_app()
