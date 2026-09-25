"""Liveness vs readiness: draining after SIGTERM is not ready but still alive (a failing liveness
probe would get the pod killed mid-drain); a consumer that died on its own is neither."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def probe(*, stopping: bool, consumer_done: bool) -> tuple[int, int]:
    app = create_app()
    consumer = SimpleNamespace(done=lambda: consumer_done)
    app.state.worker, app.state.consumer = SimpleNamespace(stopping=stopping), consumer
    client = TestClient(app)  # no lifespan: the state above stands in for it
    return client.get("/livez").status_code, client.get("/readyz").status_code


@pytest.mark.parametrize(
    ("stopping", "consumer_done", "live", "ready"),
    [
        (False, False, 200, 200),  # working
        (True, False, 200, 503),   # draining
        (True, True, 200, 503),    # drained, shutting down
        (False, True, 503, 503),   # the consumer died on its own: restart
    ],
)
def test_probes(stopping, consumer_done, live, ready):
    assert probe(stopping=stopping, consumer_done=consumer_done) == (live, ready)


def test_not_ready_before_the_consumer_starts():
    client = TestClient(create_app())
    assert (client.get("/livez").status_code, client.get("/readyz").status_code) == (200, 503)
