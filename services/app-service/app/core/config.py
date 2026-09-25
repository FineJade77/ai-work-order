import socket
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings

# How long agent-runtime may keep retrying one task before it gives up on its own, from its defaults:
# max_deliveries x (claim_idle + reclaim scan interval + task_timeout) = 3 x (90 + 10 + 60).
# Services share no code, so this is restated here; keep it in step with agent-runtime's Settings.
AGENT_RUNTIME_MAX_RETRY_SECONDS = 480


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    jwt_secret: str = Field(min_length=32)
    ticket_service_url: str
    ticket_service_token: str
    # Approvals (HITL) live in tool-gateway; handlers list and decide them through app-service.
    tool_gateway_url: str
    tool_gateway_service_token: str

    jwt_ttl_seconds: int = 8 * 3600
    # A turn still queued this long has probably lost its task (e.g. Redis failover), so it is
    # enqueued again. Must exceed agent-runtime's own retries (AGENT_RUNTIME_MAX_RETRY_SECONDS);
    # otherwise a task that is only slow gets a duplicate that races the original.
    requeue_after_seconds: float = Field(default=600, ge=AGENT_RUNTIME_MAX_RETRY_SECONDS)
    max_requeues: int = 2
    requeue_interval_seconds: float = 30
    sse_max_seconds: float = 300
    # Messages one user may send; each costs two or three model calls. Days start at midnight here.
    message_rate_per_minute: int = Field(default=6, ge=1)
    message_rate_per_day: int = Field(default=200, ge=1)
    rate_limit_timezone: str = "Asia/Shanghai"
    consumer_name: str = Field(default_factory=socket.gethostname)


@lru_cache
def get_settings() -> Settings:
    return Settings()
