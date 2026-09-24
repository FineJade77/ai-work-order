"""Integration tests against a running stack: `docker compose up -d postgres tool-gateway`
plus migrations applied. Skipped automatically when the gateway isn't reachable."""

import uuid
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from tests.integration.env import ADMIN_DB_URL, GATEWAY_URL, ONCALL_ANSWER, ROLE, SERVICE_TOKEN


@pytest.fixture(scope="session")
def gateway():
    try:
        httpx.get(f"{GATEWAY_URL}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"tool-gateway not reachable at {GATEWAY_URL}: {exc}")
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    with httpx.Client(base_url=GATEWAY_URL, headers=headers, timeout=10) as client:
        yield client


@pytest.fixture(scope="session")
def db():
    # Superuser connection: bypasses RLS, used only for fixtures and assertions.
    with psycopg.connect(ADMIN_DB_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def tenant(db):
    tenant_id = str(uuid.uuid4())
    db.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_id, f"it-{tenant_id[:8]}"))
    user_id = db.execute(
        "INSERT INTO users (tenant_id, name, email) VALUES (%s, 'Test', 'test@example.com') RETURNING id",
        (tenant_id,),
    ).fetchone()[0]
    session_id = str(
        db.execute(
            "INSERT INTO conversations (tenant_id, user_id) VALUES (%s, %s) RETURNING id", (tenant_id, user_id)
        ).fetchone()[0]
    )
    for tool in ("search_knowledge", "create_ticket"):
        db.execute(
            "INSERT INTO role_tool_bindings (tenant_id, agent_role, tool_name) VALUES (%s, %s, %s)",
            (tenant_id, ROLE, tool),
        )
    db.execute(
        "INSERT INTO knowledge_items (tenant_id, question, answer) VALUES (%s, %s, %s), (%s, %s, %s)",
        (tenant_id, "VPN 连不上怎么办？", "请重启 VPN 客户端后重试。", tenant_id, "夜间 IT 值班联系方式", ONCALL_ANSWER),
    )
    yield SimpleNamespace(id=tenant_id, session_id=session_id)
    db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))  # cascades to every row created by the tests


@pytest.fixture
def call(gateway, tenant):
    def _call(tool, args, *, key=None, role=ROLE, client=None):
        headers = {"Idempotency-Key": key} if key else {}
        body = {
            "tenant_id": tenant.id,
            "session_id": tenant.session_id,
            "agent_role": role,
            "tool_name": tool,
            "arguments": args,
        }
        return (client or gateway).post("/v1/tools/call", json=body, headers=headers)

    return _call
