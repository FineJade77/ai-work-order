from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    ticket_service_token: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
