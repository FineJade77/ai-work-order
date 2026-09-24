from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    app_service_internal_token: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
