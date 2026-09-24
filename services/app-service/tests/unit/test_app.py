import uuid

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.main import create_app

pytestmark = pytest.mark.anyio

SETTINGS = Settings(
    database_url="unused", redis_url="unused", jwt_secret="s" * 32,
    ticket_service_url="http://unused", ticket_service_token="unused",
)
ID = uuid.uuid4()


@pytest.fixture
async def client():
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_healthz(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/me"),
        ("POST", "/v1/conversations"),
        ("GET", "/v1/conversations"),
        ("GET", f"/v1/conversations/{ID}/messages"),
        ("POST", f"/v1/conversations/{ID}/messages"),
        ("GET", f"/v1/conversations/{ID}/stream?task_id={ID}"),
        ("GET", "/v1/tickets"),
        ("GET", f"/v1/tickets/{ID}"),
        ("POST", f"/v1/tickets/{ID}/transitions"),
    ],
)
@pytest.mark.parametrize("authorization", [None, "Bearer not-a-jwt", "Basic abc"])
async def test_everything_but_login_requires_a_valid_token(client, method, path, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    resp = await client.request(method, path, headers=headers, json={"content": "x"} if method == "POST" else None)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_TOKEN"
