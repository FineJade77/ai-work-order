import json
import time
from collections.abc import AsyncIterator

from app.clients.agent_bus import TERMINAL_EVENTS, AgentBus, event_stream_key
from app.db.models import Turn

READ_BLOCK_MS = 15_000  # also the keepalive interval while nothing happens


def _frame(event: str, data: str, event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {data}\n\n"


async def relay(bus: AgentBus, turn: Turn, last_event_id: str | None, max_seconds: float) -> AsyncIterator[str]:
    """SSE frames for one turn, read from the conversation's event stream.

    Reads from the start of the stream (or after Last-Event-ID), so events written before the
    browser connected are not lost. Ends after the turn's done/error event.
    """
    key, task_id = event_stream_key(turn.conversation_id), str(turn.id)
    cursor = last_event_id or "0-0"
    finished = turn.status != "queued"
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        response = await bus.redis.xread({key: cursor}, count=100, block=None if finished else READ_BLOCK_MS)
        entries = _entries(response)
        for entry_id, fields in entries:
            cursor = entry_id
            if fields.get("task_id") != task_id:
                continue
            yield _frame(fields["type"], fields["data"], entry_id)
            if fields["type"] in TERMINAL_EVENTS:
                return
        if finished and not entries:
            # The turn ended but its events are gone (expired after 24h, or trimmed): answer from the database.
            yield _final_frame(turn)
            return
        if not entries:
            yield ": keepalive\n\n"


def _final_frame(turn: Turn) -> str:
    if turn.status == "done":
        return _frame("done", json.dumps({"outcome": turn.outcome}))
    return _frame("error", json.dumps({"code": turn.error_code or "TASK_FAILED", "message": "processing failed"}))


def _entries(response) -> list:
    # RESP2: [[key, entries]]; RESP3: {key: entries}. Only one key is read.
    if not response:
        return []
    return next(iter(response.values())) if isinstance(response, dict) else response[0][1]
