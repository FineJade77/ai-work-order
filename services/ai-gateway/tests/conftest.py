import os

import pytest

# app.providers builds its client at import time and requires a key; unit tests never call out.
os.environ.setdefault("MODEL_API_KEY", "unused")


@pytest.fixture
def anyio_backend():
    return "asyncio"
