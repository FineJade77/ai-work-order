"""Runs against the built container (`docker compose up -d knowledge-service`). The point is
packaging: the JSON data file must ship inside the wheel, which unit tests can't show."""

import os

import httpx
import pytest

URL = os.environ.get("KNOWLEDGE_SERVICE_URL", "http://localhost:8005")
TOKEN = os.environ.get("KNOWLEDGE_SERVICE_TOKEN", "dev-knowledge-service-token")


@pytest.fixture(scope="module")
def client():
    try:
        httpx.get(f"{URL}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"knowledge-service not reachable at {URL}: {exc}")
    with httpx.Client(base_url=URL, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        yield c


def test_container_serves_packaged_data(client):
    resp = client.get(
        "/internal/v1/knowledge/search",
        params={"tenant_id": "e0e112f3-5afb-4229-869b-c0bd012324bb", "q": "VPN 连不上"},
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["id"] == "vpn-001"
