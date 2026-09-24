import uuid

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.main import create_app
from app.services import ticket_service

TOKEN = "unit-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+psycopg://unused", ticket_service_token=TOKEN
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.anyio
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
async def test_requires_service_token(client, headers):
    assert (await client.get(f"/internal/v1/tickets?tenant_id={uuid.uuid4()}", headers=headers)).status_code == 401


@pytest.mark.anyio
async def test_create_requires_idempotency_key(client):
    body = {"tenant_id": str(uuid.uuid4()), "requester_id": str(uuid.uuid4()), "category": "vpn", "summary": "x"}
    assert (await client.post("/internal/v1/tickets", json=body, headers=AUTH)).status_code == 422


@pytest.mark.anyio
async def test_unknown_action_is_rejected_by_schema(client):
    body = {"tenant_id": str(uuid.uuid4()), "action": "delete", "actor_id": str(uuid.uuid4()), "expected_version": 1}
    resp = await client.post(f"/internal/v1/tickets/{uuid.uuid4()}/transitions", json=body, headers=AUTH)
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_api_error_maps_to_status_and_error_body(client, monkeypatch):
    async def conflict(**_):
        raise ApiError(409, "VERSION_CONFLICT", "stale")

    monkeypatch.setattr(ticket_service, "transition", conflict)
    body = {"tenant_id": str(uuid.uuid4()), "action": "start", "actor_id": str(uuid.uuid4()), "expected_version": 1}
    resp = await client.post(f"/internal/v1/tickets/{uuid.uuid4()}/transitions", json=body, headers=AUTH)
    assert resp.status_code == 409
    assert resp.json() == {"error": {"code": "VERSION_CONFLICT", "message": "stale"}}


def test_cursor_round_trip_and_rejects_garbage():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    ticket = SimpleNamespace(created_at=datetime(2026, 9, 24, tzinfo=timezone.utc), id=uuid.uuid4())
    created_at, ticket_id = ticket_service.decode_cursor(ticket_service.encode_cursor(ticket))
    assert (created_at, ticket_id) == (ticket.created_at, ticket.id)
    with pytest.raises(ApiError):
        ticket_service.decode_cursor("not-a-cursor")
