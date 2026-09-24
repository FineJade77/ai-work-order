"""Ticket lifecycle rules. Pure functions: no database, no HTTP.

    open --start--> in_progress --resolve--> resolved --close--> closed
    open --cancel--> closed          resolved --reopen--> open
"""

from dataclasses import dataclass

STATUSES = ("open", "in_progress", "resolved", "closed")

# action -> (statuses it may start from, status it leads to)
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "start": (("open",), "in_progress"),
    "resolve": (("in_progress",), "resolved"),
    "close": (("resolved",), "closed"),
    "reopen": (("resolved",), "open"),
    "cancel": (("open",), "closed"),
}

ACTIONS = tuple(TRANSITIONS)

# Only the employee who filed the ticket may withdraw it, confirm the fix, or say it isn't fixed.
# start/resolve are IT-staff actions; roles live in app-service, which authorizes those.
REQUESTER_ONLY = frozenset({"cancel", "close", "reopen"})


class TransitionError(Exception):
    def __init__(self, http_status: int, code: str, message: str):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Transition:
    to_status: str
    assignee_id: str | None


def plan(
    *,
    action: str,
    current_status: str,
    actor_id: str,
    requester_id: str,
    current_assignee_id: str | None,
    assignee_id: str | None,
    note: str | None,
) -> Transition:
    """Validates one action against the ticket's current state and returns the resulting state."""
    allowed_from, to_status = TRANSITIONS[action]
    if current_status not in allowed_from:
        raise TransitionError(409, "INVALID_TRANSITION", f"cannot {action} a ticket that is {current_status}")
    if action in REQUESTER_ONLY and actor_id != requester_id:
        raise TransitionError(403, "NOT_REQUESTER", f"only the employee who filed the ticket may {action} it")
    if action == "start" and not assignee_id:
        raise TransitionError(400, "ASSIGNEE_REQUIRED", "start requires assignee_id")
    if action == "resolve" and not (note and note.strip()):
        raise TransitionError(400, "NOTE_REQUIRED", "resolve requires a note describing the fix")
    return Transition(to_status=to_status, assignee_id=assignee_id or current_assignee_id)
