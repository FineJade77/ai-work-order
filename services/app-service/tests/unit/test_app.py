import httpx
import pytest

from app.main import create_app


@pytest.mark.anyio
async def test_healthz():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
