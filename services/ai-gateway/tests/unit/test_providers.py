from types import SimpleNamespace

import anyio
import pytest

from app import providers
from app.providers import OpenAICompatibleProvider

pytestmark = pytest.mark.anyio

TENANT = "e0e112f3-5afb-4229-869b-c0bd012324bb"


def _chunk(content=None, usage=None):
    choices = [SimpleNamespace(delta=SimpleNamespace(content=content))] if content else []
    return SimpleNamespace(choices=choices, usage=usage)


class FakeCompletions:
    def __init__(self, chunks, *, then_hang=False, then_fail=False):
        self._chunks = chunks
        self._then_hang = then_hang
        self._then_fail = then_fail

    async def create(self, **_):
        async def stream():
            for chunk in self._chunks:
                yield chunk
            if self._then_hang:
                await anyio.sleep_forever()
            if self._then_fail:
                raise RuntimeError("upstream reset")

        return stream()


@pytest.fixture
def recorded(monkeypatch):
    rows = []

    async def record_usage(**row):
        # A real DB write awaits I/O; this await is where an unshielded cancellation would land.
        await anyio.sleep(0.01)
        rows.append(row)

    monkeypatch.setattr(providers, "record_usage", record_usage)
    return rows


def make_provider(completions):
    provider = OpenAICompatibleProvider(base_url="http://unused", api_key="unused", model="test-model")
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return provider


def generate(provider):
    return provider.generate(messages=[{"role": "user", "content": "hi"}], system_prompt="", tenant_id=TENANT)


async def test_completed_stream_records_usage(recorded):
    usage = SimpleNamespace(prompt_tokens=7, completion_tokens=2)
    provider = make_provider(FakeCompletions([_chunk("he"), _chunk("llo"), _chunk(usage=usage)]))

    events = [e async for e in generate(provider)]

    assert events == [
        {"delta": "he"},
        {"delta": "llo"},
        {"done": True, "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
    ]
    assert [(r["status"], r["prompt_tokens"], r["completion_tokens"]) for r in recorded] == [("ok", 7, 2)]


async def test_usage_is_recorded_when_the_client_disconnects_mid_stream(recorded):
    provider = make_provider(FakeCompletions([_chunk("partial")], then_hang=True))

    # What Starlette does on http.disconnect: cancel the task that is iterating the stream.
    events = []
    with anyio.CancelScope() as scope:
        async for event in generate(provider):
            events.append(event)
            scope.cancel()

    assert scope.cancelled_caught
    assert events == [{"delta": "partial"}]
    assert len(recorded) == 1 and recorded[0]["tenant_id"] == TENANT


async def test_upstream_failure_is_recorded_as_error(recorded):
    provider = make_provider(FakeCompletions([_chunk("partial")], then_fail=True))

    with pytest.raises(RuntimeError):
        async for _ in generate(provider):
            pass

    assert [r["status"] for r in recorded] == ["error"]
