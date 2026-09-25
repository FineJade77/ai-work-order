"""Logs as one JSON object per line on stdout, carrying the current trace and span ids so a log
line leads straight to its trace.

Tracing, metrics and log export are set up outside the code: `opentelemetry-instrument` wraps the
server (see the Dockerfile) and reads OTEL_* environment variables. With OTEL_SDK_DISABLED=true
(the image default) nothing is exported and no ids appear, but logs are still JSON. The one
exception is the FastAPI instrumentation, applied here by instrument_app (see there).

Every service carries its own copy of this module; services share no code.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import TracerProvider

# Attributes every LogRecord has; anything else on a record came from `extra=` and is logged.
_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}
# Added by others: OTel's log correlation (the ids are written under our own names instead) and
# uvicorn (a copy of the message with terminal colours).
_IGNORED_ATTRS = frozenset({"otelSpanID", "otelTraceID", "otelTraceSampled", "otelServiceName", "color_message"})


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            entry["trace_id"] = format(context.trace_id, "032x")
            entry["span_id"] = format(context.span_id, "016x")
        for key, value in vars(record).items():
            if key not in _RECORD_ATTRS and key not in _IGNORED_ATTRS and not key.startswith("_"):
                entry[key] = value if value is None or isinstance(value, (str, int, float, bool)) else str(value)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(default_service: str) -> None:
    """Idempotent. Keeps other root handlers, notably the one OTel adds to export log records."""
    service = os.environ.get("OTEL_SERVICE_NAME", default_service)
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h.formatter, JsonFormatter)]
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    # uvicorn gives its loggers their own plain-text handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def instrument_app(app: FastAPI, tracer_provider: TracerProvider | None = None) -> None:
    """One server span per request. Done here rather than by the automatic instrumentation (the
    Dockerfile turns that off) because only here can the ASGI send/receive spans be dropped: one
    per message, noise for a JSON API and one span per event on an SSE stream. Probes are left
    out too."""
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="healthz", exclude_spans=["receive", "send"], tracer_provider=tracer_provider
    )
