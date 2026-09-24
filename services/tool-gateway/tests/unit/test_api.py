import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.errors import CallerError
from app.main import create_app
from app.services import tool_call_service

TOKEN = "unit-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(tool_gateway_service_token=TOKEN)
    return TestClient(app)


def test_healthz_needs_no_token(client):
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_v1_rejects_missing_or_wrong_token(client, headers):
    assert client.get("/v1/tools/list?tenant_id=x&agent_role=y", headers=headers).status_code == 401


def test_list_with_valid_token(client, monkeypatch):
    monkeypatch.setattr(tool_call_service, "list_tools", lambda t, r: [{"type": "function"}])
    resp = client.get("/v1/tools/list?tenant_id=x&agent_role=y", headers=AUTH)
    assert resp.status_code == 200 and resp.json() == {"tools": [{"type": "function"}]}


def test_caller_error_maps_to_http_status_and_error_body(client, monkeypatch):
    def raise_conflict(**_):
        raise CallerError(409, "IDEMPOTENCY_CONFLICT", "reused key")

    monkeypatch.setattr(tool_call_service, "call_tool", raise_conflict)
    resp = client.post(
        "/v1/tools/call",
        headers=AUTH,
        json={"tenant_id": "t", "session_id": "s", "agent_role": "r", "tool_name": "x"},
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": {"code": "IDEMPOTENCY_CONFLICT", "message": "reused key"}}
