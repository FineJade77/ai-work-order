from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    tool_gateway_service_token: str
    database_url: str
    # Downstream systems the tools call. Each has its own service token.
    knowledge_service_url: str
    knowledge_service_token: str
    ticket_service_url: str
    ticket_service_token: str
    # Approval decisions are published to the approval_events stream here.
    redis_url: str
    # A call waiting for approval expires, with its approval, after this long.
    approval_ttl_hours: float = Field(default=24, gt=0)
    # How often the relay looks for decisions to publish and approvals to expire.
    approval_relay_interval_seconds: float = Field(default=2, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
