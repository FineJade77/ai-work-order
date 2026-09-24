import pytest
from pydantic import ValidationError

from app.core.config import Settings

REQUIRED = dict(
    redis_url="redis://r",
    checkpoint_database_url="postgresql://c",
    tool_gateway_url="http://t",
    tool_gateway_service_token="t",
    ai_gateway_url="http://a",
    ai_gateway_service_token="a",
)


def test_defaults_are_consistent():
    s = Settings(**REQUIRED)
    assert s.task_timeout_seconds < s.conversation_lock_seconds < s.claim_idle_seconds


@pytest.mark.parametrize(
    "timing",
    [
        {"task_timeout_seconds": 80},  # a live run could outlast its lock
        {"conversation_lock_seconds": 120},  # a reclaimed task would bounce off a dead worker's lock
    ],
)
def test_inconsistent_timing_is_rejected(timing):
    with pytest.raises(ValidationError):
        Settings(**REQUIRED, **timing)
