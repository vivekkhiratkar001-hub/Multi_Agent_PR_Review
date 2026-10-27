import pytest
from pydantic import SecretStr, ValidationError

from pr_reviewer.config import Settings


def test_defaults_are_safe():
    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.llm_provider == "gemini"
    assert settings.enable_github_publishing is False


def test_model_name_is_trimmed():
    settings = Settings(_env_file=None, llm_model="  test-model  ")

    assert settings.llm_model == "test-model"


def test_empty_model_name_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_model="   ")


def test_selected_gemini_key():
    key = SecretStr("example-gemini-key")
    settings = Settings(
        _env_file=None,
        llm_provider="gemini",
        gemini_api_key=key,
    )

    assert settings.selected_llm_api_key == key


def test_selected_openrouter_key():
    key = SecretStr("example-openrouter-key")
    settings = Settings(
        _env_file=None,
        llm_provider="openrouter",
        openrouter_api_key=key,
    )

    assert settings.selected_llm_api_key == key


def test_publishing_is_disabled_by_default():
    settings = Settings(_env_file=None)

    assert settings.enable_github_publishing is False
