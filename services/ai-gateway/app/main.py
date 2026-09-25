import json
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app import budget
from app.core.config import Settings, get_settings
from app.core.telemetry import configure_logging, instrument_app
from app.providers import ModelProvider, get_provider
from app.schemas import GenerateRequest
from app.security import require_service_token
from app.tenancy import parse_tenant_id

logger = logging.getLogger("ai_gateway")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_provider()  # fail at startup, not on the first request, if config is missing
    yield


configure_logging("ai-gateway")
app = FastAPI(title="ai-gateway", lifespan=lifespan)
instrument_app(app)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/generate", dependencies=[Depends(require_service_token)], response_model=None)
async def generate(
    req: GenerateRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    provider: Annotated[ModelProvider, Depends(get_provider)],
) -> StreamingResponse | JSONResponse:
    # Both checks happen here, before the response starts: once the text/event-stream is under
    # way, a refusal could only abort it mid-flight instead of returning a clean status.
    try:
        parse_tenant_id(req.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        await budget.check_budget(req.tenant_id, settings)
    except budget.BudgetExceeded as exc:
        logger.warning("token budget exceeded tenant_id=%s used=%d limit=%d", req.tenant_id, exc.used, exc.limit)
        return JSONResponse(
            status_code=429,
            content={"error": {"code": "TOKEN_BUDGET_EXCEEDED", "message": "the tenant's daily token budget is used up"}},
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )

    async def event_stream():
        # aclosing: `async for` alone never closes the provider's generator, so its usage write
        # would wait for garbage collection if this stream were abandoned between chunks.
        async with aclosing(
            provider.generate(
                messages=[m.model_dump() for m in req.messages],
                system_prompt=req.system_prompt,
                tenant_id=req.tenant_id,
            )
        ) as events:
            async for event in events:
                yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
