import dataclasses
import uuid

import pytest

from app.core.errors import CallerError
from app.repositories.tool_calls import CallRecord
from app.services import tool_call_service as svc
from app.tools.registry import REGISTRY

TENANT = str(uuid.uuid4())
SESSION = str(uuid.uuid4())
USER = str(uuid.uuid4())
ROLE = "it_support_agent"
TICKET_ARGS = {"summary": "VPN 连不上", "category": "vpn"}


class FakeToolCalls:
    """In-memory stand-in for app.repositories.tool_calls with the same semantics."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def find_by_idempotency_key(self, tenant_id, key):
        row = self.rows.get(key)
        return CallRecord(**{k: v for k, v in row.items() if k != "key"}) if row else None

    async def claim(self, *, tenant_id, session_id, user_id, agent_role, tool_name, arguments, request_hash, idempotency_key):
        if idempotency_key in self.rows:
            return None
        call_id = str(uuid.uuid4())
        self.rows[idempotency_key] = dict(
            id=call_id, key=idempotency_key, tool_name=tool_name, arguments=arguments,
            request_hash=request_hash, status="running", result=None, error_code=None, error_message=None,
        )
        return call_id

    def _by_id(self, call_id):
        return next(r for r in self.rows.values() if r["id"] == call_id)

    async def reclaim(self, tenant_id, call_id):
        row = self._by_id(call_id)
        if row["status"] != "error":
            return False
        row["status"] = "running"
        return True

    async def complete(self, tenant_id, call_id, result):
        self._by_id(call_id).update(status="success", result=result)

    async def fail(self, tenant_id, call_id, code, message):
        self._by_id(call_id).update(status="error", error_code=code, error_message=message)


@pytest.fixture
def env(monkeypatch):
    fake = FakeToolCalls()
    audits: list[dict] = []
    handler_calls: list[dict] = []

    async def ticket_handler(ctx, args):
        handler_calls.append(args)
        return {"ticket_id": "T-1", "token": "leaked"}

    async def search_handler(ctx, args):
        return {"results": []}

    async def bound_tool_names(tenant_id, agent_role):
        return {"search_knowledge", "create_ticket"} if agent_role == ROLE else set()

    async def write_audit(**row):
        audits.append(row)

    monkeypatch.setitem(REGISTRY, "create_ticket", dataclasses.replace(REGISTRY["create_ticket"], handler=ticket_handler))
    monkeypatch.setitem(REGISTRY, "search_knowledge", dataclasses.replace(REGISTRY["search_knowledge"], handler=search_handler))
    monkeypatch.setattr(svc.bindings, "bound_tool_names", bound_tool_names)
    monkeypatch.setattr(svc.audit, "write", write_audit)
    for name in ("find_by_idempotency_key", "claim", "reclaim", "complete", "fail"):
        monkeypatch.setattr(svc.tool_calls, name, getattr(fake, name))
    return fake, audits, handler_calls


async def call(tool="create_ticket", args=TICKET_ARGS, key="k1", role=ROLE, tenant=TENANT, user=USER):
    return await svc.call_tool(
        tenant_id=tenant,
        session_id=SESSION,
        user_id=user,
        agent_role=role,
        tool_name=tool,
        arguments=args,
        idempotency_key=key,
    )


@pytest.mark.anyio
async def test_invalid_tenant_is_a_caller_error_and_not_audited(env):
    _, audits, _ = env
    with pytest.raises(CallerError) as exc:
        await call(tenant="not-a-uuid")
    assert exc.value.http_status == 400
    assert audits == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"tool": "drop_database", "args": {}}, "UNKNOWN_TOOL"),
        ({"role": "guest"}, "TOOL_NOT_ALLOWED"),
        ({"args": {"summary": "x", "category": "printer"}}, "INVALID_ARGUMENTS"),
    ],
)
async def test_policy_failures_are_model_visible_errors_audited_as_rejected(env, kwargs, code):
    _, audits, handler_calls = env
    outcome = await call(**kwargs)
    assert (outcome.status, outcome.error_code) == ("error", code)
    assert audits[-1]["status"] == "rejected"
    assert handler_calls == []


@pytest.mark.anyio
async def test_side_effect_tool_requires_idempotency_key(env):
    _, audits, _ = env
    with pytest.raises(CallerError) as exc:
        await call(key=None)
    assert (exc.value.http_status, exc.value.code) == (400, "MISSING_IDEMPOTENCY_KEY")
    assert audits[-1]["error_code"] == "MISSING_IDEMPOTENCY_KEY"


@pytest.mark.anyio
async def test_read_only_tool_is_not_recorded(env):
    fake, audits, _ = env
    outcome = await call(tool="search_knowledge", args={"query": "VPN"}, key=None)
    assert outcome.status == "success"
    assert fake.rows == {}
    assert audits[-1]["status"] == "success"


@pytest.mark.anyio
async def test_retry_with_same_key_replays_without_rerunning_the_handler(env):
    _, _, handler_calls = env
    first = await call()
    second = await call()
    assert first.status == second.status == "success"
    assert second.replayed and second.result == first.result and second.call_id == first.call_id
    assert len(handler_calls) == 1


@pytest.mark.anyio
async def test_secret_fields_in_results_are_masked(env):
    assert (await call()).result == {"ticket_id": "T-1", "token": "***"}


@pytest.mark.anyio
async def test_same_key_with_different_arguments_is_a_conflict(env):
    await call()
    with pytest.raises(CallerError) as exc:
        await call(args={"summary": "different", "category": "vpn"})
    assert (exc.value.http_status, exc.value.code) == (409, "IDEMPOTENCY_CONFLICT")


@pytest.mark.anyio
async def test_call_still_running_elsewhere_is_in_progress(env):
    fake, _, _ = env
    await fake.claim(tenant_id=TENANT, session_id=SESSION, user_id=USER, agent_role=ROLE, tool_name="create_ticket",
                     arguments=TICKET_ARGS, request_hash=svc.request_hash(SESSION, USER, ROLE, "create_ticket", TICKET_ARGS),
                     idempotency_key="k1")
    with pytest.raises(CallerError) as exc:
        await call()
    assert (exc.value.http_status, exc.value.code) == (409, "IN_PROGRESS")


@pytest.mark.anyio
async def test_internal_failure_is_masked_and_retryable(env, monkeypatch):
    _, _, handler_calls = env
    attempts = iter([RuntimeError("connection to 10.0.3.7:5432 refused"), None])

    async def flaky(ctx, args):
        err = next(attempts)
        if err:
            raise err
        handler_calls.append(args)
        return {"ticket_id": "T-2"}

    monkeypatch.setitem(REGISTRY, "create_ticket", dataclasses.replace(REGISTRY["create_ticket"], handler=flaky))
    failed = await call()
    assert (failed.status, failed.error_code, failed.error_message) == ("error", "TOOL_ERROR", "tool execution failed")
    retried = await call()
    assert retried.status == "success" and retried.result == {"ticket_id": "T-2"}


def test_request_hash_ignores_key_order():
    a = svc.request_hash(SESSION, USER, ROLE, "create_ticket", {"summary": "s", "category": "vpn"})
    b = svc.request_hash(SESSION, USER, ROLE, "create_ticket", {"category": "vpn", "summary": "s"})
    assert a == b


@pytest.mark.anyio
async def test_invalid_user_is_a_caller_error_and_audited(env):
    _, audits, handler_calls = env
    with pytest.raises(CallerError) as exc:
        await call(user="not-a-uuid")
    assert (exc.value.http_status, exc.value.code) == (400, "INVALID_USER")
    assert audits[-1]["error_code"] == "INVALID_USER"
    assert handler_calls == []


@pytest.mark.anyio
async def test_same_key_from_a_different_user_is_a_conflict(env):
    await call()
    with pytest.raises(CallerError) as exc:
        await call(user=str(uuid.uuid4()))
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_handler_receives_the_user_as_context(env, monkeypatch):
    seen = []

    async def handler(ctx, args):
        seen.append(ctx.user_id)
        return {"ticket_id": "T"}

    monkeypatch.setitem(REGISTRY, "create_ticket", dataclasses.replace(REGISTRY["create_ticket"], handler=handler))
    await call()
    assert seen == [USER]
