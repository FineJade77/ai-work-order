import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import CallerError
from app.core.telemetry import configure_logging, instrument_app
from app.services.approval_relay import ApprovalRelay

logger = logging.getLogger("tool_gateway")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # fail at startup, not on the first request, if config is missing
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    relay = asyncio.create_task(ApprovalRelay(redis, settings.approval_relay_interval_seconds).run())
    relay.add_done_callback(_log_relay_exit)
    app.state.relay = relay
    try:
        yield
    finally:
        relay.cancel()
        await asyncio.gather(relay, return_exceptions=True)
        await redis.aclose()


def _log_relay_exit(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        logger.error("approval relay stopped", exc_info=task.exception())


async def caller_error_handler(_: Request, exc: CallerError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code, "message": exc.message}})


def create_app() -> FastAPI:
    app = FastAPI(title="tool-gateway", lifespan=lifespan)
    app.add_exception_handler(CallerError, caller_error_handler)

    @app.get("/healthz")
    async def healthz(response: Response) -> dict[str, str]:
        relay = getattr(app.state, "relay", None)
        if relay is not None and relay.done():  # decisions would never reach the agent
            response.status_code = 503
            return {"status": "approval relay stopped"}
        return {"status": "ok"}

    app.include_router(api_router)
    instrument_app(app)
    return app


configure_logging("tool-gateway")
app = create_app()
