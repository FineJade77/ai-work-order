import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from shared.tenancy import parse_tenant_id

from app.providers import provider
from app.schemas import GenerateRequest

app = FastAPI(title="ai-gateway")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/generate")
async def generate(req: GenerateRequest) -> StreamingResponse:
    # Validated here (not just inside the provider) so a bad tenant_id gets a clean 400
    # instead of aborting an already-started text/event-stream response mid-flight.
    try:
        parse_tenant_id(req.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def event_stream():
        async for event in provider.generate(
            messages=[m.model_dump() for m in req.messages],
            system_prompt=req.system_prompt,
            tenant_id=req.tenant_id,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
