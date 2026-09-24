import pytest

from app.domain.state_machine import STATUSES, TRANSITIONS, TransitionError, plan

REQUESTER = "requester-1"
STAFF = "it-staff-1"

VALID = [
    ("start", "open", "in_progress", STAFF),
    ("resolve", "in_progress", "resolved", STAFF),
    ("close", "resolved", "closed", REQUESTER),
    ("reopen", "resolved", "open", REQUESTER),
    ("cancel", "open", "closed", REQUESTER),
]


def _plan(action, current, actor, assignee="it-staff-1", note="fixed"):
    return plan(
        action=action,
        current_status=current,
        actor_id=actor,
        requester_id=REQUESTER,
        current_assignee_id=None,
        assignee_id=assignee,
        note=note,
    )


@pytest.mark.parametrize(("action", "current", "expected", "actor"), VALID)
def test_valid_transitions(action, current, expected, actor):
    assert _plan(action, current, actor).to_status == expected


def test_every_other_combination_is_rejected():
    valid = {(a, c) for a, c, _, _ in VALID}
    for action in TRANSITIONS:
        for current in STATUSES:
            if (action, current) in valid:
                continue
            with pytest.raises(TransitionError) as exc:
                _plan(action, current, REQUESTER)
            assert (exc.value.http_status, exc.value.code) == (409, "INVALID_TRANSITION")


@pytest.mark.parametrize(("action", "current"), [("close", "resolved"), ("reopen", "resolved"), ("cancel", "open")])
def test_requester_only_actions_reject_anyone_else(action, current):
    with pytest.raises(TransitionError) as exc:
        _plan(action, current, STAFF)
    assert (exc.value.http_status, exc.value.code) == (403, "NOT_REQUESTER")


def test_start_requires_assignee():
    with pytest.raises(TransitionError) as exc:
        _plan("start", "open", STAFF, assignee=None)
    assert exc.value.code == "ASSIGNEE_REQUIRED"


@pytest.mark.parametrize("note", [None, "", "   "])
def test_resolve_requires_note(note):
    with pytest.raises(TransitionError) as exc:
        _plan("resolve", "in_progress", STAFF, note=note)
    assert exc.value.code == "NOTE_REQUIRED"


def test_assignee_is_kept_when_not_given():
    result = plan(
        action="resolve", current_status="in_progress", actor_id=STAFF, requester_id=REQUESTER,
        current_assignee_id="it-staff-1", assignee_id=None, note="fixed",
    )
    assert result.assignee_id == "it-staff-1"
