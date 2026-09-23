FROM python:3.11-slim

WORKDIR /srv

COPY libs/shared /shared-src
RUN pip install --no-cache-dir /shared-src

COPY services/tool-gateway/pyproject.toml ./
COPY services/tool-gateway/app ./app
RUN pip install --no-cache-dir .

EXPOSE 8003
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
