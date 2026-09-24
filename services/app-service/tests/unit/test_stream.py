import json
import uuid
from types import SimpleNamespace

import pytest

from app.clients.agent_bus import event_stream_key
from app.services.stream_service import relay

pytestmark = pytest.mark.anyio


class FakeRedis:
    """Serves scripted XREAD batches and records the cursors it was asked for."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.reads = []

    async def xread(self, streams, count, block):
        [(key, cursor)] = streams.items()
        self.reads.append((key, cursor, block))
        entries = self.batches.pop(0) if self.batches else []
        return [[key, entries]] if entries else []


def turn(status="queued", outcome=None, error_code=None):
    return SimpleNamespace(id=uuid.uuid4(), conversation_id=uuid.uuid4(), status=status, outcome=outcome, error_code=error_code)


def event(entry_id, t, type_, **data):
    return (entry_id, {"task_id": str(t.id), "type": type_, "data": json.dumps(data)})


async def frames(redis, t, last_event_id=None):
    return [f async for f in relay(SimpleNamespace(redis=redis), t, last_event_id, max_seconds=5)]


async def test_relays_this_turns_events_until_done():
    t, other = turn(), turn()
    redis = FakeRedis([
        [event("1-0", other, "answer_chunk", delta="not mine"), event("2-0", t, "retrieving")],
        [],
        [event("3-0", t, "answer", content="hi"), event("4-0", t, "done", outcome="awaiting_feedback"), event("5-0", t, "retrieving")],
    ])
    out = await frames(redis, t)
    assert out == [
        'id: 2-0\nevent: retrieving\ndata: {}\n\n',
        ": keepalive\n\n",
        'id: 3-0\nevent: answer\ndata: {"content": "hi"}\n\n',
        'id: 4-0\nevent: done\ndata: {"outcome": "awaiting_feedback"}\n\n',
    ]
    assert redis.reads[0][:2] == (event_stream_key(t.conversation_id), "0-0")
    assert redis.reads[2][1] == "2-0"  # the cursor advances past entries of other turns too


async def test_resumes_after_last_event_id():
    t = turn()
    redis = FakeRedis([[event("9-0", t, "done", outcome="replied")]])
    await frames(redis, t, last_event_id="8-0")
    assert redis.reads[0][1] == "8-0"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("done", 'event: done\ndata: {"outcome": "ticket_created"}\n\n'),
        ("failed", 'event: error\ndata: {"code": "NO_RESULT", "message": "processing failed"}\n\n'),
    ],
)
async def test_finished_turn_with_expired_events_is_answered_from_the_database(status, expected):
    t = turn(status=status, outcome="ticket_created", error_code="NO_RESULT")
    redis = FakeRedis([])
    assert await frames(redis, t) == [expected]
    assert redis.reads[0][2] is None  # a finished turn is never waited on
