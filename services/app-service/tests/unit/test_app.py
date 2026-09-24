from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz():
    assert TestClient(create_app()).get("/healthz").json() == {"status": "ok"}
