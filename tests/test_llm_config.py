import pytest

from adapters.llm import smoke
from adapters.llm.config import LLMSettings
from domain.errors import LLMConfigurationError

MINIMAL_ENV = {"LLM_PRIMARY_API_KEYS": "k1", "LLM_PRIMARY_MODEL": "some/model"}


def test_defaults_from_minimal_env() -> None:
    settings = LLMSettings.from_env(MINIMAL_ENV)

    assert settings.primary.name == "eurouter"
    assert settings.primary.base_url == "https://api.eurouter.ai/api/v1"
    assert settings.primary.model == "some/model"
    assert settings.primary.api_keys == ("k1",)
    assert settings.fallback is None
    assert settings.timeout_seconds == 120
    assert settings.temperature == 0.0
    assert settings.json_mode is True


def test_missing_api_keys_names_the_variable() -> None:
    with pytest.raises(LLMConfigurationError, match="LLM_PRIMARY_API_KEYS"):
        LLMSettings.from_env({"LLM_PRIMARY_MODEL": "m"})


def test_missing_model_names_the_variable() -> None:
    with pytest.raises(LLMConfigurationError, match="LLM_PRIMARY_MODEL"):
        LLMSettings.from_env({"LLM_PRIMARY_API_KEYS": "k1"})


def test_blank_keys_are_dropped() -> None:
    settings = LLMSettings.from_env({**MINIMAL_ENV, "LLM_PRIMARY_API_KEYS": "k1, ,k2"})

    assert settings.primary.api_keys == ("k1", "k2")


@pytest.mark.parametrize("raw", [",", " , ,"])
def test_only_blank_keys_is_an_error(raw: str) -> None:
    with pytest.raises(LLMConfigurationError, match="LLM_PRIMARY_API_KEYS"):
        LLMSettings.from_env({**MINIMAL_ENV, "LLM_PRIMARY_API_KEYS": raw})


def test_fallback_enabled_without_model_is_an_error() -> None:
    with pytest.raises(LLMConfigurationError, match="LLM_FALLBACK_MODEL"):
        LLMSettings.from_env({**MINIMAL_ENV, "LLM_FALLBACK_ENABLED": "true"})


def test_fallback_settings() -> None:
    settings = LLMSettings.from_env(
        {
            **MINIMAL_ENV,
            "LLM_FALLBACK_ENABLED": "true",
            "LLM_FALLBACK_MODEL": "qwen2.5-coder:7b",
            "LLM_FALLBACK_BASE_URL": "http://ollama:11434/v1/",
        }
    )

    assert settings.fallback is not None
    assert settings.fallback.name == "ollama"
    assert settings.fallback.base_url == "http://ollama:11434/v1"
    assert settings.fallback.api_keys == ()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLM_TIMEOUT_SECONDS", "soon"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("LLM_TEMPERATURE", "-0.1"),
        ("LLM_TEMPERATURE", "3"),
        ("LLM_JSON_MODE", "maybe"),
        ("LLM_FALLBACK_ENABLED", "sure"),
        ("LLM_PRIMARY_BASE_URL", "api.eurouter.ai"),
    ],
)
def test_malformed_values_name_the_variable(name: str, value: str) -> None:
    with pytest.raises(LLMConfigurationError, match=name):
        LLMSettings.from_env({**MINIMAL_ENV, name: value})


def test_json_mode_can_be_disabled() -> None:
    assert LLMSettings.from_env({**MINIMAL_ENV, "LLM_JSON_MODE": "false"}).json_mode is False


def test_smoke_without_env_reports_missing_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("LLM_PRIMARY_API_KEYS", "LLM_PRIMARY_MODEL", "LLM_FALLBACK_ENABLED"):
        monkeypatch.delenv(name, raising=False)

    exit_code = smoke.main(["tests/fixtures/sql_injection.diff"])

    assert exit_code == smoke.EXIT_CONFIG_ERROR
    assert "LLM_PRIMARY_API_KEYS" in capsys.readouterr().err
