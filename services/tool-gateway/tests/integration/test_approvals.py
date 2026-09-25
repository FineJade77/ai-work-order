"""HITL approvals against the running gateway, Postgres and Redis: a call that needs approval waits,
runs exactly once when approved, never when rejected or expired, and every decision is announced
on the approval_events stream by the outbox relay."""

import time
import uuid
from types import SimpleNamespace

import pytest
from redis import Redis

from tests.integration.calls import make_call
from tests.integration.env import REDIS_URL, ROLE

GRANT = {"system": "财务系统", "access_level": "read", "reason": "月底对账需要查看报表"}


def _bind(db, tenant_id: str, *, requires_approval: bool) -> None:
    db.execute(
        "INSERT INTO role_tool_bindings (tenant_id, agent_role, tool_name, requires_approval)"
        " VALUES (%s, %s, %s, %s)",
        (tenant_id, ROLE, "grant_access", requires_approval),
    )


@pytest.fixture
def approving(db):
    """A throwaway tenant whose policy makes grant_access wait for a handler."""
    t = SimpleNamespace(
        id=str(uuid.uuid4()), user=str(uuid.uuid4()), session=str(uuid.uuid4()), handler=str(uuid.uuid4())
    )
    _bind(db, t.id, requires_approval=True)
    yield t
    _cleanup(db, t.id)


@pytest.fixture
def direct(db):
    """A throwaway tenant whose policy lets grant_access run straight away."""
    t = SimpleNamespace(id=str(uuid.uuid4()), user=str(uuid.uuid4()), session=str(uuid.uuid4()))
    _bind(db, t.id, requires_approval=False)
    yield t
    _cleanup(db, t.id)


def _cleanup(db, tenant_id: str) -> None:
    for table in (
        "approval_outbox", "tool_approvals", "access_grants", "tool_call_logs", "tool_calls", "role_tool_bindings"
    ):
        db.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))


@pytest.fixture(scope="session")
def redis():
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    client.close()


def decide(gateway, t, approval_id, decision, approver, note=None):
    body = {"tenant_id": t.id, "decision": decision, "approver_id": approver, "note": note}
    return gateway.post(f"/v1/approvals/{approval_id}/decision", json=body)


def event_for(redis, approval_id: str, status: str, timeout: float = 15) -> dict:
    """Waits for the relay to publish this approval's decision."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for _, fields in redis.xrevrange("approval_events", count=200):
            if fields["approval_id"] == approval_id and fields["status"] == status:
                return fields
        time.sleep(0.2)
    raise AssertionError(f"no {status} event for approval {approval_id}")


def grants(db, tenant_id: str) -> int:
    return db.execute("SELECT count(*) FROM access_grants WHERE tenant_id = %s", (tenant_id,)).fetchone()[0]


def open_expiry_checks(db, approval_id: str) -> int:
    return db.execute(
        "SELECT count(*) FROM approval_outbox WHERE approval_id = %s AND kind = 'expire' AND done_at IS NULL",
        (approval_id,),
    ).fetchone()[0]


def test_an_approved_call_runs_once_the_agent_calls_again(gateway, db, redis, approving):
    call, key = make_call(gateway, approving), str(uuid.uuid4())
    first = call("grant_access", GRANT, key=key).json()
    assert first["status"] == "pending_approval"
    approval_id = first["approval"]["id"]
    assert call("grant_access", GRANT, key=key).json()["status"] == "pending_approval"  # still waiting
    assert grants(db, approving.id) == 0

    [listed] = gateway.get("/v1/approvals", params={"tenant_id": approving.id, "status": "pending"}).json()["items"]
    assert (listed["id"], listed["arguments"], listed["requester_id"]) == (approval_id, GRANT, approving.user)
    assert listed["conversation_id"] == approving.session

    own = decide(gateway, approving, approval_id, "approve", approver=approving.user)
    assert (own.status_code, own.json()["error"]["code"]) == (403, "SELF_APPROVAL")
    approved = decide(gateway, approving, approval_id, "approve", approver=approving.handler, note="已核实")
    assert (approved.status_code, approved.json()["status"]) == (200, "approved")

    event = event_for(redis, approval_id, "approved")
    assert (event["conversation_id"], event["requester_id"], event["decided_by"]) == (
        approving.session, approving.user, approving.handler,
    )
    assert event["traceparent"].startswith("00-")  # the relay's span, parent of whoever consumes it

    done = call("grant_access", GRANT, key=key).json()
    assert (done["status"], done["result"]["status"], done["result"]["system"]) == ("success", "granted", "财务系统")
    assert call("grant_access", GRANT, key=key).json()["replayed"] is True
    assert grants(db, approving.id) == 1

    again = decide(gateway, approving, approval_id, "reject", approver=approving.handler)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "APPROVAL_NOT_PENDING")


def test_a_rejected_call_never_runs(gateway, db, redis, approving):
    call, key = make_call(gateway, approving), str(uuid.uuid4())
    approval_id = call("grant_access", GRANT, key=key).json()["approval"]["id"]
    assert open_expiry_checks(db, approval_id) == 1
    decide(gateway, approving, approval_id, "reject", approver=approving.handler, note="权限需部门经理申请")
    event_for(redis, approval_id, "rejected")

    for replayed in (False, True):  # the refusal is final: calling again does not run the tool
        body = call("grant_access", GRANT, key=key).json()
        assert (body["status"], body["error"]["code"], body["replayed"]) == ("error", "APPROVAL_REJECTED", replayed)
        assert "权限需部门经理申请" in body["error"]["message"]
    assert grants(db, approving.id) == 0
    assert open_expiry_checks(db, approval_id) == 0  # decided: no longer counted as pending


def test_an_approval_nobody_decides_expires(gateway, db, redis, approving):
    call, key = make_call(gateway, approving), str(uuid.uuid4())
    approval_id = call("grant_access", GRANT, key=key).json()["approval"]["id"]
    # Fast-forward: the approval and its expiry check are now overdue.
    db.execute("UPDATE tool_approvals SET expires_at = now() - interval '1 second' WHERE id = %s", (approval_id,))
    db.execute("UPDATE approval_outbox SET due_at = now() - interval '1 second' WHERE approval_id = %s", (approval_id,))

    event_for(redis, approval_id, "expired")
    late = decide(gateway, approving, approval_id, "approve", approver=approving.handler)
    assert (late.status_code, late.json()["error"]["code"]) == (409, "APPROVAL_NOT_PENDING")
    body = call("grant_access", GRANT, key=key).json()
    assert (body["status"], body["error"]["code"]) == ("error", "APPROVAL_EXPIRED")
    assert grants(db, approving.id) == 0


def test_approving_after_the_deadline_is_refused_even_before_the_relay_expires_it(gateway, db, approving):
    call = make_call(gateway, approving)
    approval_id = call("grant_access", GRANT, key=str(uuid.uuid4())).json()["approval"]["id"]
    db.execute("UPDATE tool_approvals SET expires_at = now() - interval '1 second' WHERE id = %s", (approval_id,))
    late = decide(gateway, approving, approval_id, "approve", approver=approving.handler)
    assert (late.status_code, late.json()["error"]["code"]) == (409, "APPROVAL_EXPIRED")


def test_the_same_tool_runs_straight_away_under_another_tenants_policy(gateway, db, direct):
    body = make_call(gateway, direct)("grant_access", GRANT, key=str(uuid.uuid4())).json()
    assert (body["status"], body["result"]["status"]) == ("success", "granted")
    assert gateway.get("/v1/approvals", params={"tenant_id": direct.id}).json()["items"] == []


def test_the_waiting_call_is_audited_as_pending_approval(gateway, db, approving):
    make_call(gateway, approving)("grant_access", GRANT, key=str(uuid.uuid4()))
    [status] = db.execute(
        "SELECT status FROM tool_call_logs WHERE tenant_id = %s AND tool_name = 'grant_access'", (approving.id,)
    ).fetchone()
    assert status == "pending_approval"
