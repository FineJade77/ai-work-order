FROM python:3.11-slim

WORKDIR /srv

COPY pyproject.toml ./
COPY shared ./shared
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

CMD ["alembic", "upgrade", "head"]
