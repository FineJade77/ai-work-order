"""Integration tests against Postgres (app_service migrated), Redis and, for tickets, a running
ticket-service (`docker compose up -d ticket-service`). The app runs in-process with its lifespan,
so the result collector and requeuer are live. Skipped when a dependency is unreachable."""

import uuid
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import session_factory
from app.main import create_app
from tests.integration.env import (
    ADMIN_DB_URL,
    DATABASE_URL,
    PASSWORD,
    REDIS_URL,
    TICKET_ADMIN_DB_URL,
    TICKET_SERVICE_TOKEN,
    TICKET_SERVICE_URL,
)


@pytest.fixture(scope="session")
def admin_db():
    try:
        conn = psycopg.connect(ADMIN_DB_URL, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"app_service database not reachable: {exc}")
    with conn:
        yield conn


@pytest.fixture
def tenant(admin_db):
    """A throwaway tenant with two employees and one handler, removed afterwards."""
    t = SimpleNamespace(id=uuid.uuid4(), slug=f"it-{uuid.uuid4().hex[:12]}")
    admin_db.execute("INSERT INTO tenants (id, name, slug) VALUES (%s, %s, %s)", (t.id, "Integration", t.slug))
    password_hash = hash_password(PASSWORD, n=2**14)  # cheap: the stored hash carries its own cost
    for name, role in (("alice", "employee"), ("bob", "employee"), ("ivan", "handler")):
        user = SimpleNamespace(id=uuid.uuid4(), email=f"{name}@{t.slug}.example", role=role)
        admin_db.execute(
            "INSERT INTO users (id, tenant_id, name, email, role, password_hash) VALUES (%s, %s, %s, %s, %s, %s)",
            (user.id, t.id, name, user.email, role, password_hash),
        )
        setattr(t, name, user)
    yield t
    admin_db.execute("DELETE FROM tenants WHERE id = %s", (t.id,))  # cascades to users, conversations, turns
    with psycopg.connect(TICKET_ADMIN_DB_URL, autocommit=True) as ticket_db:
        ticket_db.execute("DELETE FROM tickets WHERE tenant_id = %s", (t.id,))


@pytest.fixture
async def redis():
    client = Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=20)
    try:
        await client.ping()
    except OSError as exc:
        await client.aclose()
        pytest.skip(f"Redis not reachable at {REDIS_URL}: {exc}")
    await client.flushdb()  # the dedicated test database only
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def app(monkeypatch, redis, tenant):
    env = {
        "DATABASE_URL": DATABASE_URL,
        "REDIS_URL": REDIS_URL,
        "JWT_SECRET": "integration-secret-" + "x" * 32,
        "TICKET_SERVICE_URL": TICKET_SERVICE_URL,
        "TICKET_SERVICE_TOKEN": TICKET_SERVICE_TOKEN,
        "CONSUMER_NAME": "integration",
        # Only the startup sweep runs; tests that need the requeuer call sweep() themselves.
        "REQUEUE_INTERVAL_SECONDS": "3600",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    session_factory.cache_clear()  # the engine belongs to the previous test's event loop
    application = create_app()
    async with application.router.lifespan_context(application):
        yield application
    await session_factory().kw["bind"].dispose()
    session_factory.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30) as c:
        yield c


@pytest.fixture
def login(client, tenant):
    async def _login(user) -> dict[str, str]:
        resp = await client.post("/v1/auth/login", json={"tenant": tenant.slug, "email": user.email, "password": PASSWORD})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    return _login
