from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import CallerError
from app.core.telemetry import configure_logging, instrument_app


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_settings()  # fail at startup, not on the first request, if config is missing
    yield


async def caller_error_handler(_: Request, exc: CallerError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code, "message": exc.message}})


def create_app() -> FastAPI:
    app = FastAPI(title="tool-gateway", lifespan=lifespan)
    app.add_exception_handler(CallerError, caller_error_handler)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api_router)
    instrument_app(app)
    return app


configure_logging("tool-gateway")
app = create_app()
