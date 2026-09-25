import httpx
import pytest

from app.core.config import Settings, get_settings
from app.core.errors import CallerError
from app.main import create_app
from app.services import tool_call_service

pytestmark = pytest.mark.anyio

TOKEN = "unit-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        tool_gateway_service_token=TOKEN,
        database_url="postgresql+psycopg://unused",
        knowledge_service_url="http://unused",
        knowledge_service_token="unused",
        ticket_service_url="http://unused",
        ticket_service_token="unused",
        redis_url="redis://unused",
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_healthz_needs_no_token(client):
    assert (await client.get("/healthz")).status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
async def test_v1_rejects_missing_or_wrong_token(client, headers):
    assert (await client.get("/v1/tools/list?tenant_id=x&agent_role=y", headers=headers)).status_code == 401


async def test_list_with_valid_token(client, monkeypatch):
    async def list_tools(tenant_id, agent_role):
        return [{"type": "function"}]

    monkeypatch.setattr(tool_call_service, "list_tools", list_tools)
    resp = await client.get("/v1/tools/list?tenant_id=x&agent_role=y", headers=AUTH)
    assert resp.status_code == 200 and resp.json() == {"tools": [{"type": "function"}]}


async def test_caller_error_maps_to_http_status_and_error_body(client, monkeypatch):
    async def raise_conflict(**_):
        raise CallerError(409, "IDEMPOTENCY_CONFLICT", "reused key")

    monkeypatch.setattr(tool_call_service, "call_tool", raise_conflict)
    resp = await client.post(
        "/v1/tools/call",
        headers=AUTH,
        json={"tenant_id": "t", "session_id": "s", "user_id": "u", "agent_role": "r", "tool_name": "x"},
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": {"code": "IDEMPOTENCY_CONFLICT", "message": "reused key"}}
