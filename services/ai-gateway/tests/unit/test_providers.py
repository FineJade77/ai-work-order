import json
from types import SimpleNamespace

import anyio
import pytest
from opentelemetry.trace import SpanKind, StatusCode

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


def make_provider(completions, *, capture_content=False):
    provider = OpenAICompatibleProvider(
        base_url="http://unused", api_key="unused", model="test-model", capture_content=capture_content
    )
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


async def test_usage_is_not_recorded_when_the_client_disconnects_mid_stream(recorded):
    # Known gap, accepted on purpose: the write is not shielded, so the cancellation aborts it.
    # If disconnected calls must be recorded, wrap the write in anyio.CancelScope(shield=True).
    provider = make_provider(FakeCompletions([_chunk("partial")], then_hang=True))

    # What Starlette does on http.disconnect: cancel the task that is iterating the stream.
    events = []
    with anyio.CancelScope() as scope:
        async for event in generate(provider):
            events.append(event)
            scope.cancel()

    assert scope.cancelled_caught
    assert events == [{"delta": "partial"}]
    assert recorded == []


async def test_upstream_failure_is_recorded_as_error(recorded):
    provider = make_provider(FakeCompletions([_chunk("partial")], then_fail=True))

    with pytest.raises(RuntimeError):
        async for _ in generate(provider):
            pass

    assert [r["status"] for r in recorded] == ["error"]


def _chat_span(spans):
    [span] = [s for s in spans.get_finished_spans() if s.name == "chat test-model"]
    return span


async def test_a_model_call_is_a_genai_span_without_content_by_default(recorded, spans):
    usage = SimpleNamespace(prompt_tokens=7, completion_tokens=2)
    chunks = [_chunk("he"), _chunk("llo"), _chunk(usage=usage)]
    [e async for e in generate(make_provider(FakeCompletions(chunks)))]

    span = _chat_span(spans)
    assert span.kind == SpanKind.CLIENT
    attrs = span.attributes
    assert (attrs["gen_ai.operation.name"], attrs["gen_ai.request.model"], attrs["app.tenant_id"]) == (
        "chat", "test-model", TENANT,
    )
    assert (attrs["gen_ai.usage.input_tokens"], attrs["gen_ai.usage.output_tokens"]) == (7, 2)
    assert "app.time_to_first_token_ms" in attrs and [e.name for e in span.events] == ["gen_ai.first_token"]
    content_keys = ("gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions")
    assert not any(k in attrs for k in content_keys)


async def test_content_is_recorded_masked_only_when_switched_on(recorded, spans):
    provider = make_provider(FakeCompletions([_chunk("已记下 13812345678")]), capture_content=True)
    messages = [{"role": "user", "content": "我的手机 13812345678"}]
    [e async for e in provider.generate(messages=messages, system_prompt="你是助手", tenant_id=TENANT)]

    attrs = _chat_span(spans).attributes
    assert json.loads(attrs["gen_ai.input.messages"]) == [
        {"role": "user", "parts": [{"type": "text", "content": "我的手机 138****5678"}]}
    ]
    assert json.loads(attrs["gen_ai.output.messages"])[0]["parts"][0]["content"] == "已记下 138****5678"
    assert json.loads(attrs["gen_ai.system_instructions"]) == [{"type": "text", "content": "你是助手"}]


async def test_a_failed_model_call_is_an_error_span(recorded, spans):
    with pytest.raises(RuntimeError):
        [e async for e in generate(make_provider(FakeCompletions([_chunk("partial")], then_fail=True)))]
    assert _chat_span(spans).status.status_code == StatusCode.ERROR
