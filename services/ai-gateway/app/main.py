import json
from contextlib import aclosing

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from app.providers import provider
from app.schemas import GenerateRequest
from app.security import require_service_token
from app.tenancy import parse_tenant_id

app = FastAPI(title="ai-gateway")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/generate", dependencies=[Depends(require_service_token)])
async def generate(req: GenerateRequest) -> StreamingResponse:
    # Validated here (not just inside the provider) so a bad tenant_id gets a clean 400
    # instead of aborting an already-started text/event-stream response mid-flight.
    try:
        parse_tenant_id(req.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
