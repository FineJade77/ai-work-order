import dataclasses
import uuid

import pytest

from app.core.errors import CallerError
from app.repositories.tool_calls import CallRecord
from app.services import tool_call_service as svc
from app.tools.registry import REGISTRY

TENANT = str(uuid.uuid4())
SESSION = str(uuid.uuid4())
ROLE = "it_support_agent"
TICKET_ARGS = {"summary": "VPN 连不上", "category": "vpn"}


class FakeToolCalls:
    """In-memory stand-in for app.repositories.tool_calls with the same semantics."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def find_by_idempotency_key(self, tenant_id, key):
        row = self.rows.get(key)
        return CallRecord(**{k: v for k, v in row.items() if k != "key"}) if row else None

    def claim(self, *, tenant_id, session_id, agent_role, tool_name, arguments, request_hash, idempotency_key):
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

    def reclaim(self, tenant_id, call_id):
        row = self._by_id(call_id)
        if row["status"] != "error":
            return False
        row["status"] = "running"
        return True

    def complete(self, tenant_id, call_id, result):
        self._by_id(call_id).update(status="success", result=result)

    def fail(self, tenant_id, call_id, code, message):
        self._by_id(call_id).update(status="error", error_code=code, error_message=message)


@pytest.fixture
def env(monkeypatch):
    fake = FakeToolCalls()
    audits: list[dict] = []
    handler_calls: list[dict] = []

    def ticket_handler(ctx, args):
        handler_calls.append(args)
        return {"ticket_id": "T-1", "token": "leaked"}

    monkeypatch.setitem(REGISTRY, "create_ticket", dataclasses.replace(REGISTRY["create_ticket"], handler=ticket_handler))
    monkeypatch.setitem(
        REGISTRY, "search_knowledge", dataclasses.replace(REGISTRY["search_knowledge"], handler=lambda c, a: {"results": []})
    )
    monkeypatch.setattr(svc.bindings, "bound_tool_names", lambda t, r: {"search_knowledge", "create_ticket"} if r == ROLE else set())
    monkeypatch.setattr(svc.audit, "write", lambda **kw: audits.append(kw))
    for name in ("find_by_idempotency_key", "claim", "reclaim", "complete", "fail"):
        monkeypatch.setattr(svc.tool_calls, name, getattr(fake, name))
    return fake, audits, handler_calls


def call(tool="create_ticket", args=TICKET_ARGS, key="k1", role=ROLE, tenant=TENANT):
    return svc.call_tool(
        tenant_id=tenant, session_id=SESSION, agent_role=role, tool_name=tool, arguments=args, idempotency_key=key
    )


def test_invalid_tenant_is_a_caller_error_and_not_audited(env):
    _, audits, _ = env
    with pytest.raises(CallerError) as exc:
        call(tenant="not-a-uuid")
    assert exc.value.http_status == 400
    assert audits == []


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"tool": "drop_database", "args": {}}, "UNKNOWN_TOOL"),
        ({"role": "guest"}, "TOOL_NOT_ALLOWED"),
        ({"args": {"summary": "x", "category": "printer"}}, "INVALID_ARGUMENTS"),
    ],
)
def test_policy_failures_are_model_visible_errors_audited_as_rejected(env, kwargs, code):
    _, audits, handler_calls = env
    outcome = call(**kwargs)
    assert (outcome.status, outcome.error_code) == ("error", code)
    assert audits[-1]["status"] == "rejected"
    assert handler_calls == []


def test_side_effect_tool_requires_idempotency_key(env):
    _, audits, _ = env
    with pytest.raises(CallerError) as exc:
        call(key=None)
    assert (exc.value.http_status, exc.value.code) == (400, "MISSING_IDEMPOTENCY_KEY")
    assert audits[-1]["error_code"] == "MISSING_IDEMPOTENCY_KEY"


def test_read_only_tool_is_not_recorded(env):
    fake, audits, _ = env
    outcome = call(tool="search_knowledge", args={"query": "VPN"}, key=None)
    assert outcome.status == "success"
    assert fake.rows == {}
    assert audits[-1]["status"] == "success"


def test_retry_with_same_key_replays_without_rerunning_the_handler(env):
    _, _, handler_calls = env
    first = call()
    second = call()
    assert first.status == second.status == "success"
    assert second.replayed and second.result == first.result and second.call_id == first.call_id
    assert len(handler_calls) == 1


def test_secret_fields_in_results_are_masked(env):
    assert call().result == {"ticket_id": "T-1", "token": "***"}


def test_same_key_with_different_arguments_is_a_conflict(env):
    call()
    with pytest.raises(CallerError) as exc:
        call(args={"summary": "different", "category": "vpn"})
    assert (exc.value.http_status, exc.value.code) == (409, "IDEMPOTENCY_CONFLICT")


def test_call_still_running_elsewhere_is_in_progress(env):
    fake, _, _ = env
    fake.claim(tenant_id=TENANT, session_id=SESSION, agent_role=ROLE, tool_name="create_ticket",
               arguments=TICKET_ARGS, request_hash=svc.request_hash(SESSION, ROLE, "create_ticket", TICKET_ARGS),
               idempotency_key="k1")
    with pytest.raises(CallerError) as exc:
        call()
    assert (exc.value.http_status, exc.value.code) == (409, "IN_PROGRESS")


def test_internal_failure_is_masked_and_retryable(env, monkeypatch):
    _, _, handler_calls = env
    attempts = iter([RuntimeError("connection to 10.0.3.7:5432 refused"), None])

    def flaky(ctx, args):
        err = next(attempts)
        if err:
            raise err
        handler_calls.append(args)
        return {"ticket_id": "T-2"}

    monkeypatch.setitem(REGISTRY, "create_ticket", dataclasses.replace(REGISTRY["create_ticket"], handler=flaky))
    failed = call()
    assert (failed.status, failed.error_code, failed.error_message) == ("error", "TOOL_ERROR", "tool execution failed")
    retried = call()
    assert retried.status == "success" and retried.result == {"ticket_id": "T-2"}


def test_request_hash_ignores_key_order():
    a = svc.request_hash(SESSION, ROLE, "create_ticket", {"summary": "s", "category": "vpn"})
    b = svc.request_hash(SESSION, ROLE, "create_ticket", {"category": "vpn", "summary": "s"})
    assert a == b
