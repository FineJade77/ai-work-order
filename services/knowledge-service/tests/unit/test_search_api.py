import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app

TOKEN = "unit-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DEMO = "e0e112f3-5afb-4229-869b-c0bd012324bb"
NORTHWIND = "9b2d6c1a-4e7f-4a3b-8c5d-2f1e0a9b8c7d"


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(knowledge_service_token=TOKEN)
    return TestClient(app)


def search(client, tenant_id, q, **params):
    return client.get("/internal/v1/knowledge/search", params={"tenant_id": tenant_id, "q": q, **params}, headers=AUTH)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_requires_service_token(client, headers):
    resp = client.get("/internal/v1/knowledge/search", params={"tenant_id": DEMO, "q": "vpn"}, headers=headers)
    assert resp.status_code == 401


def test_each_tenant_has_its_own_domain_and_categories(client):
    """demo is an IT help desk, northwind an HR/admin one: categories are whatever the data says."""
    it = search(client, DEMO, "VPN 连不上").json()["results"][0]
    hr = search(client, NORTHWIND, "年假怎么申请").json()["results"][0]
    assert (it["id"], it["category"]) == ("vpn-001", "vpn")
    assert (hr["id"], hr["category"]) == ("leave-001", "leave")


def test_items_of_one_tenant_never_appear_for_another(client):
    assert search(client, NORTHWIND, "VPN 连不上").json()["results"] == []
    assert search(client, DEMO, "年假怎么申请").json()["results"] == []


def test_unknown_tenant_gets_empty_results(client):
    assert search(client, "00000000-0000-4000-8000-000000000000", "VPN").json() == {"results": []}


def test_tenant_id_is_matched_case_insensitively(client):
    assert search(client, DEMO.upper(), "VPN 连不上").json()["results"]


def test_invalid_tenant_is_400(client):
    resp = search(client, "not-a-uuid", "VPN")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "INVALID_TENANT"


def test_limit_and_result_shape(client):
    results = search(client, DEMO, "VPN", limit=2).json()["results"]
    assert len(results) == 2
    assert set(results[0]) == {"id", "category", "question", "answer", "score"}


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "x" * 201}, {"q": "vpn", "limit": 0}, {"q": "vpn", "limit": 21}])
def test_query_bounds_are_validated(client, params):
    resp = client.get("/internal/v1/knowledge/search", params={"tenant_id": DEMO, **params}, headers=AUTH)
    assert resp.status_code == 422
