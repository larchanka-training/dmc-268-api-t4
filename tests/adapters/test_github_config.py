"""Reading GitHubAppSettings from the environment."""

from pathlib import Path

import pytest

from adapters.github.config import GitHubAppSettings
from domain.errors import ForgeConfigurationError

KEY_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "github" / "app_key.pem"
WEBHOOK_SECRET = "whsec_test_secret_value"


def env(**overrides: str) -> dict[str, str]:
    base = {
        "GITHUB_APP_ID": "123456",
        "GITHUB_APP_PRIVATE_KEY_PATH": str(KEY_PATH),
        "GITHUB_APP_SLUG": "review-agent",
        "GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET,
    }
    return {**base, **overrides}


def test_the_webhook_secret_is_read_from_the_environment() -> None:
    settings = GitHubAppSettings.from_env(env())

    assert settings.webhook_secret == WEBHOOK_SECRET
    assert settings.api_base_url == "https://api.github.com"


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_webhook_secret_is_a_configuration_error(value: str) -> None:
    with pytest.raises(ForgeConfigurationError, match="GITHUB_WEBHOOK_SECRET"):
        GitHubAppSettings.from_env(env(GITHUB_WEBHOOK_SECRET=value))


def test_a_missing_webhook_secret_is_named_in_the_error() -> None:
    variables = env()
    del variables["GITHUB_WEBHOOK_SECRET"]

    with pytest.raises(ForgeConfigurationError, match="GITHUB_WEBHOOK_SECRET"):
        GitHubAppSettings.from_env(variables)


def test_the_webhook_secret_never_appears_in_a_repr() -> None:
    settings = GitHubAppSettings.from_env(env())

    assert WEBHOOK_SECRET not in repr(settings)
