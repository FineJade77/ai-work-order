from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import budget, main
from app.providers import get_provider

pytestmark = pytest.mark.anyio

TENANT = "e0e112f3-5afb-4229-869b-c0bd012324bb"
AUTH = {"Authorization": "Bearer unit-test-token"}
BODY = {"tenant_id": TENANT, "messages": [{"role": "user", "content": "x"}]}


class FakeProvider:
    def __init__(self):
        self.calls = 0

    async def generate(self, **_):
        self.calls += 1
        yield {"delta": "hi"}
        yield {"done": True, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


@pytest.fixture
def provider():
    fake = FakeProvider()
    main.app.dependency_overrides[get_provider] = lambda: fake
    yield fake
    main.app.dependency_overrides.clear()


@pytest.fixture
def budget_left(monkeypatch):
    """The budget check needs the database; unit tests decide its answer instead."""
    state = {"exceeded": None}

    async def check_budget(tenant_id, settings, **_):
        if state["exceeded"]:
            raise state["exceeded"]

    monkeypatch.setattr(budget, "check_budget", check_budget)
    return state


@pytest.fixture
async def client(provider, budget_left):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as c:
        yield c


async def test_healthz(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "unit-test-token"}])
async def test_generate_rejects_missing_or_wrong_token(client, headers):
    resp = await client.post("/v1/generate", json={"tenant_id": TENANT, "messages": []}, headers=headers)
    assert resp.status_code == 401


async def test_invalid_tenant_is_400_before_streaming(client):
    resp = await client.post("/v1/generate", json={"tenant_id": "not-a-uuid", "messages": []}, headers=AUTH)
    assert resp.status_code == 400


async def test_generate_streams_provider_events_as_sse(client):
    resp = await client.post("/v1/generate", json=BODY, headers=AUTH)

    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text.split("\n\n")[:2] == [
        'data: {"delta": "hi"}',
        'data: {"done": true, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}',
    ]


async def test_exhausted_budget_is_a_429_before_any_model_call(client, provider, budget_left):
    budget_left["exceeded"] = budget.BudgetExceeded(used=1200, limit=1000, retry_after_seconds=3600)
    resp = await client.post("/v1/generate", json=BODY, headers=AUTH)

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "TOKEN_BUDGET_EXCEEDED"
    assert resp.headers["Retry-After"] == "3600"
    assert provider.calls == 0


def test_budget_day_runs_from_local_midnight_to_local_midnight():
    shanghai = ZoneInfo("Asia/Shanghai")
    # 17:30 UTC is already 01:30 the next day in Shanghai.
    start, end = budget.day_bounds(datetime(2026, 9, 24, 17, 30, tzinfo=ZoneInfo("UTC")), "Asia/Shanghai")
    assert (start, end) == (datetime(2026, 9, 25, tzinfo=shanghai), datetime(2026, 9, 26, tzinfo=shanghai))
