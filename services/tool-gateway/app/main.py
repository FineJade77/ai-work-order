import contextlib

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp_server import mcp_server

# DNS-rebinding host checking guards against malicious *browser pages* calling a
# localhost-bound dev server. Tool Gateway is only ever called server-to-server by
# Agent Runtime inside the private network (architecture-design.md section 2's
# call-relationship constraint), never from a browser, so that threat doesn't apply here.
mcp_app = mcp_server.streamable_http_app(
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp_app.router.lifespan_context(mcp_app):
        yield


app = FastAPI(title="tool-gateway", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/", mcp_app)
