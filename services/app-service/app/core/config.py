import socket
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    jwt_secret: str = Field(min_length=32)
    ticket_service_url: str
    ticket_service_token: str

    jwt_ttl_seconds: int = 8 * 3600
    # A turn still queued this long has probably lost its task (e.g. Redis failover), so it is
    # enqueued again. Must exceed agent-runtime's own retries: 3 deliveries x 90s claim idle.
    requeue_after_seconds: float = 300
    max_requeues: int = 2
    requeue_interval_seconds: float = 30
    sse_max_seconds: float = 300
    consumer_name: str = Field(default_factory=socket.gethostname)


@lru_cache
def get_settings() -> Settings:
    return Settings()
