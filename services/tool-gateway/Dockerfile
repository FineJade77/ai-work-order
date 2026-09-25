FROM python:3.11-slim

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

# Unprivileged, numeric so Kubernetes can enforce runAsNonRoot. Nothing is written at runtime,
# so the root filesystem can also be mounted read-only.
RUN useradd --system --uid 10001 --no-create-home app
USER 10001

EXPOSE 8003
# Tracing, metrics and log export come from the OTel agent wrapping the server, configured by
# OTEL_* variables. Off unless the deployment turns it on (and points it at a collector), so the
# image also runs quietly on its own.
ENV OTEL_SDK_DISABLED=true
# FastAPI is instrumented in code instead (app/core/telemetry.py: instrument_app).
ENV OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi
CMD ["opentelemetry-instrument", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
