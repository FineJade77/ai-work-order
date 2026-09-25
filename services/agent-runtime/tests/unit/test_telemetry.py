import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.telemetry import JsonFormatter, configure_logging, instrument_app


def _format(message="hello", **extra) -> dict:
    record = logging.makeLogRecord({"name": "test", "levelname": "WARNING", "msg": message, **extra})
    return json.loads(JsonFormatter("svc").format(record))


def test_a_line_is_json_with_service_level_and_extras():
    line = _format("处理 %s", args=("完成",), tenant_id="t-1", attempt=2, color_message="ignored")
    assert (line["service"], line["level"], line["logger"], line["message"]) == ("svc", "WARNING", "test", "处理 完成")
    assert (line["tenant_id"], line["attempt"]) == ("t-1", 2)
    assert "color_message" not in line and "trace_id" not in line  # no span: no ids


def test_a_line_inside_a_span_carries_its_trace_and_span_ids():
    span = TracerProvider().get_tracer("test").start_span("work")
    with trace.use_span(span, end_on_exit=True):
        line = _format()
    context = span.get_span_context()
    assert line["trace_id"] == format(context.trace_id, "032x")
    assert line["span_id"] == format(context.span_id, "016x")


def test_configuring_twice_leaves_one_json_handler_and_keeps_the_others():
    root = logging.getLogger()
    other = logging.NullHandler()
    root.addHandler(other)
    try:
        configure_logging("svc")
        configure_logging("svc")
        assert sum(isinstance(h.formatter, JsonFormatter) for h in root.handlers) == 1
        assert other in root.handlers
        assert logging.getLogger("uvicorn.access").propagate and not logging.getLogger("uvicorn.access").handlers
    finally:
        root.removeHandler(other)


def test_instrumented_app_records_one_span_per_request_and_skips_probes():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = FastAPI()
    app.get("/livez")(lambda: {"status": "ok"})
    app.get("/readyz")(lambda: {"status": "ok"})
    app.get("/items/{item_id}")(lambda item_id: {"id": item_id})
    instrument_app(app, tracer_provider=provider)

    client = TestClient(app)
    client.get("/livez")
    client.get("/readyz")
    client.get("/items/7")
    assert [s.name for s in exporter.get_finished_spans()] == ["GET /items/{item_id}"]  # no send/receive
