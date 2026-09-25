import os

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Required settings; unit tests never call out.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused@localhost/unused")
os.environ.setdefault("MODEL_API_KEY", "unused")
os.environ.setdefault("AI_GATEWAY_SERVICE_TOKEN", "unit-test-token")


@pytest.fixture
def anyio_backend():
    return "asyncio"


# The global tracer provider can be set once per process: every test's spans go to this exporter,
# and tests that look at spans clear it first (the `spans` fixture).
_EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    _EXPORTER.clear()
    return _EXPORTER
