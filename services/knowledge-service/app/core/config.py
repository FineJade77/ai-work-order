from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    knowledge_service_token: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
