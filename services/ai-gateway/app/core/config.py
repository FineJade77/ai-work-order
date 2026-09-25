from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    model_api_key: str
    ai_gateway_service_token: str

    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-chat"
    # gen_ai.provider.name on model-call spans (the base URL says where, not who).
    model_provider_name: str = "deepseek"
    # Record prompts and replies on model-call spans (app/genai_tracing.py). The name is the one
    # OTel's GenAI instrumentations use, so it is the same switch everywhere.
    capture_message_content: bool = Field(
        default=False, validation_alias=AliasChoices("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")
    )
    # Tokens (prompt + completion) a tenant may use per day, unless tenant_token_budgets overrides
    # it. A soft cap: checked before each call and counted after, so concurrent calls can overshoot
    # it by what they use between them.
    daily_token_budget: int = Field(default=1_000_000, ge=0)
    # Where a budget day starts and ends.
    budget_timezone: str = "Asia/Shanghai"

    @field_validator("budget_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises for an unknown zone, so a typo fails at startup
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
