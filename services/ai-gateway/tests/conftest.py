import os

import pytest

# Required settings; unit tests never call out.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused@localhost/unused")
os.environ.setdefault("MODEL_API_KEY", "unused")
os.environ.setdefault("AI_GATEWAY_SERVICE_TOKEN", "unit-test-token")


@pytest.fixture
def anyio_backend():
    return "asyncio"
