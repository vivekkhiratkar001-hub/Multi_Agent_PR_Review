from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    llm_provider: Literal["gemini", "openrouter"] = "gemini"
    llm_model: str = "gemini-2.5-flash"
    gemini_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None

    github_webhook_secret: SecretStr | None = None
    github_token: SecretStr | None = None
    github_app_id: str | None = None
    github_private_key_path: str | None = None

    enable_github_publishing: bool = False

    @field_validator("llm_model")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("LLM_MODEL must not be empty")
        return value

    @property
    def selected_llm_api_key(self) -> SecretStr | None:
        """Return the API key for the selected provider."""
        if self.llm_provider == "gemini":
            return self.gemini_api_key
        return self.openrouter_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
