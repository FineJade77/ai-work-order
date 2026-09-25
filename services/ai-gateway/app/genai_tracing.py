"""One span per model call, following the OTel GenAI semantic conventions (gen_ai.*).

Message content (system prompt, messages, reply) is recorded only when
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT is true, the switch OTel's own GenAI
instrumentations use: on for local development, off by default elsewhere, since it puts what
employees typed into the tracing backend. Even then it is truncated and phone/email/ID-card
numbers are masked (the same rules as tool-gateway's audit redaction; services share no code).
"""

import json
import re
import time
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, StatusCode

tracer = trace.get_tracer("ai_gateway.genai")

CONTENT_MAX_CHARS = 4000

_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_CN_ID_CARD = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_CN_MOBILE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")


def mask_pii(text: str) -> str:
    # ID card before mobile, so an 18-digit ID is masked whole rather than as a partial match.
    text = _CN_ID_CARD.sub(r"\1********\2", text)
    text = _CN_MOBILE.sub(r"\1****\2", text)
    return _EMAIL.sub(r"\1***\2", text)


def _content(value: object) -> str:
    text = mask_pii(json.dumps(value, ensure_ascii=False))
    return text if len(text) <= CONTENT_MAX_CHARS else text[:CONTENT_MAX_CHARS] + "…(truncated)"


def _text_parts(text: str) -> list[dict]:
    return [{"type": "text", "content": text}]


class ChatSpan:
    """The span of one streamed chat completion. Started and ended by hand rather than with a
    context manager: the call runs inside an async generator that yields between chunks, and a
    context attached across a yield would be restored in the wrong place. `current()` makes it
    the active span only while the request is sent, so the HTTP call nests under it."""

    def __init__(
        self, *, provider: str, model: str, base_url: str, tenant_id: str, capture_content: bool,
        system_prompt: str, messages: list[dict],
    ):
        self._capture = capture_content
        self._started = time.monotonic()
        self._first_token_at: float | None = None
        self._reply: list[str] = []
        self._finish_reason: str | None = None
        self._span: Span = tracer.start_span(
            f"chat {model}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": model,
                "server.address": urlparse(base_url).hostname or "",
                "app.tenant_id": tenant_id,
            },
        )
        if capture_content:
            self._span.set_attribute("gen_ai.system_instructions", _content(_text_parts(system_prompt)))
            self._span.set_attribute(
                "gen_ai.input.messages",
                _content([{"role": m["role"], "parts": _text_parts(m["content"])} for m in messages]),
            )

    def current(self):
        return trace.use_span(self._span, end_on_exit=False)

    def on_chunk(self, chunk) -> None:
        # Read defensively: tracing must never be what breaks a model call.
        if model := getattr(chunk, "model", None):
            self._span.set_attribute("gen_ai.response.model", model)
        choices = getattr(chunk, "choices", None)
        if not choices:
            return
        choice = choices[0]
        if content := getattr(getattr(choice, "delta", None), "content", None):
            if self._first_token_at is None:
                self._first_token_at = time.monotonic()
                self._span.add_event("gen_ai.first_token")
            if self._capture:
                self._reply.append(content)
        if finish_reason := getattr(choice, "finish_reason", None):
            self._finish_reason = finish_reason

    def end(self, *, input_tokens: int, output_tokens: int, error: BaseException | None = None) -> None:
        span = self._span
        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        if self._first_token_at is not None:
            span.set_attribute("app.time_to_first_token_ms", int((self._first_token_at - self._started) * 1000))
        if self._finish_reason:
            span.set_attribute("gen_ai.response.finish_reasons", [self._finish_reason])
        if self._capture:
            reply = {"role": "assistant", "parts": _text_parts("".join(self._reply))}
            if self._finish_reason:
                reply["finish_reason"] = self._finish_reason
            span.set_attribute("gen_ai.output.messages", _content([reply]))
        if error is not None:
            span.record_exception(error)
            span.set_status(StatusCode.ERROR, str(error))
        span.end()
