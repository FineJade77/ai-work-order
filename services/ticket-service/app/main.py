from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.internal.router import internal_router
from app.core.config import get_settings
from app.core.errors import ApiError


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_settings()  # fail at startup, not on the first request, if config is missing
    yield


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code, "message": exc.message}})


def create_app() -> FastAPI:
    app = FastAPI(title="ticket-service", lifespan=lifespan)
    app.add_exception_handler(ApiError, api_error_handler)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(internal_router)
    return app


app = create_app()
