import httpx
import pytest

from app import main

pytestmark = pytest.mark.anyio

TENANT = "e0e112f3-5afb-4229-869b-c0bd012324bb"


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as c:
        yield c


async def test_healthz(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_invalid_tenant_is_400_before_streaming(client):
    resp = await client.post("/v1/generate", json={"tenant_id": "not-a-uuid", "messages": []})
    assert resp.status_code == 400


async def test_generate_streams_provider_events_as_sse(client, monkeypatch):
    async def fake_generate(**_):
        yield {"delta": "hi"}
        yield {"done": True, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(main.provider, "generate", fake_generate)
    resp = await client.post("/v1/generate", json={"tenant_id": TENANT, "messages": [{"role": "user", "content": "x"}]})

    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text.split("\n\n")[:2] == [
        'data: {"delta": "hi"}',
        'data: {"done": true, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}',
    ]
