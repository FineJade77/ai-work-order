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

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
