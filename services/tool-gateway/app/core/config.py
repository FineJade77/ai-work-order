from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    tool_gateway_service_token: str
    database_url: str
    # Downstream systems the tools call. Each has its own service token.
    knowledge_service_url: str
    knowledge_service_token: str
    ticket_service_url: str
    ticket_service_token: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
