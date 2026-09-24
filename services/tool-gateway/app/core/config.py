from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Read from TOOL_GATEWAY_SERVICE_TOKEN. APP_DATABASE_URL is read by shared.db.session.
    tool_gateway_service_token: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
