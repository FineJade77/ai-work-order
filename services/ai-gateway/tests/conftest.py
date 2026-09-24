import os

import pytest

# Both are read at import time; unit tests never call out.
os.environ.setdefault("MODEL_API_KEY", "unused")
os.environ.setdefault("AI_GATEWAY_SERVICE_TOKEN", "unit-test-token")


@pytest.fixture
def anyio_backend():
    return "asyncio"
