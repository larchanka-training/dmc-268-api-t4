"""adapters/observability: Sentry settings from the environment and the SDK options.

Every test that enables Sentry replaces ``sentry_sdk.init`` with a recorder, so no
client is ever built and nothing can reach the network (AGENTS.md hard rule 4).
"""

from typing import Any

import pytest

from adapters.observability import sentry as sentry_module
from adapters.observability.config import SentrySettings
from adapters.observability.sentry import drop_query_string, init_sentry, traces_sampler

FAKE_DSN = "https://public@o0.ingest.sentry.example/0"


@pytest.fixture
def recorded_init(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    return calls


def test_settings_default_to_off_and_local() -> None:
    settings = SentrySettings.from_env({})

    assert not settings.enabled
    assert settings.environment == "local"
    assert settings.release is None


def test_settings_read_the_dsn_env_and_image_tag() -> None:
    settings = SentrySettings.from_env(
        {"SENTRY_DSN": f" {FAKE_DSN} ", "ENV": "production", "IMAGE_TAG": "a" * 40}
    )

    assert settings.enabled
    assert settings.dsn == FAKE_DSN
    assert settings.environment == "production"
    assert settings.release == "a" * 40


def test_an_empty_dsn_is_off() -> None:
    assert not SentrySettings.from_env({"SENTRY_DSN": "  "}).enabled


def test_the_dsn_stays_out_of_the_repr() -> None:
    assert FAKE_DSN not in repr(SentrySettings(dsn=FAKE_DSN))


def test_init_without_a_dsn_does_nothing(recorded_init: list[dict[str, Any]]) -> None:
    assert init_sentry(SentrySettings()) is False
    assert recorded_init == []


def test_init_turns_off_every_default_that_leaks_data(
    recorded_init: list[dict[str, Any]],
) -> None:
    settings = SentrySettings(dsn=FAKE_DSN, environment="production", release="abc123")

    assert init_sentry(settings) is True

    [options] = recorded_init
    assert options["dsn"] == FAKE_DSN
    assert options["environment"] == "production"
    assert options["release"] == "abc123"
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["traces_sampler"] is traces_sampler
    assert options["before_send"] is drop_query_string
    assert options["before_send_transaction"] is drop_query_string


@pytest.mark.parametrize(
    ("context", "rate"),
    [
        ({"asgi_scope": {"path": "/health", "root_path": ""}}, 0.0),
        ({"asgi_scope": {"path": "/api/health", "root_path": "/api"}}, 0.0),
        ({"asgi_scope": {"path": "/api/jobs/0192f1c4/events", "root_path": "/api"}}, 0.0),
        ({"asgi_scope": {"path": "/api/jobs/0192f1c4", "root_path": "/api"}}, 1.0),
        ({"asgi_scope": {"path": "/api/webhooks/github", "root_path": "/api"}}, 1.0),
        # Worker transactions have no ASGI scope.
        ({}, 1.0),
    ],
)
def test_the_healthcheck_and_the_sse_stream_are_not_traced(
    context: dict[str, Any], rate: float
) -> None:
    assert traces_sampler(context) == rate


def test_the_query_string_is_dropped() -> None:
    event: Any = {
        "request": {
            "url": "https://example.test/api/auth/github/callback",
            "query_string": "code=gho-test-code&state=state-1",
        }
    }

    result = drop_query_string(event, {})

    assert result is not None
    assert "query_string" not in result["request"]
    assert result["request"]["url"] == "https://example.test/api/auth/github/callback"


def test_an_event_without_a_request_passes_unchanged() -> None:
    event: Any = {"message": "boom"}

    assert drop_query_string(event, {}) == {"message": "boom"}
