import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app
from app.services import ticket_service

TOKEN = "unit-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "k1"}
BODY = {"tenant_id": str(uuid.uuid4()), "conversation_id": str(uuid.uuid4()), "summary": "VPN", "category": "vpn"}


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+psycopg://unused", app_service_internal_token=TOKEN
    )
    return TestClient(app)


@pytest.mark.parametrize("headers", [{"Idempotency-Key": "k1"}, {"Authorization": "Bearer wrong", "Idempotency-Key": "k1"}])
def test_internal_routes_require_service_token(client, headers):
    assert client.post("/internal/v1/tickets", json=BODY, headers=headers).status_code == 401


def test_idempotency_key_is_required(client):
    assert client.post("/internal/v1/tickets", json=BODY, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 422


@pytest.mark.parametrize(("created", "code"), [(True, 201), (False, 200)])
def test_new_ticket_is_201_and_replay_is_200(client, monkeypatch, created, code):
    monkeypatch.setattr(ticket_service, "create_ticket", lambda **_: ticket_service.CreatedTicket("T-1", created))
    resp = client.post("/internal/v1/tickets", json=BODY, headers=AUTH)
    assert resp.status_code == code
    assert resp.json() == {"ticket_id": "T-1", "created": created}


@pytest.mark.parametrize(
    ("error", "code"),
    [(ticket_service.InvalidRequest("bad tenant"), 400), (ticket_service.ConversationNotFound("c"), 404)],
)
def test_service_errors_map_to_http_status(client, monkeypatch, error, code):
    def raise_error(**_):
        raise error

    monkeypatch.setattr(ticket_service, "create_ticket", raise_error)
    assert client.post("/internal/v1/tickets", json=BODY, headers=AUTH).status_code == code
