from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    tool_gateway_service_token: str
    database_url: str
    # app-service owns tickets; create_ticket goes through its internal API.
    app_service_url: str
    app_service_token: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
