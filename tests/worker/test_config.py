"""WorkerSettings.from_env: defaults from the docs, overrides, errors that
name their variable, and no secret in a repr."""

import pytest

from domain.errors import DBConfigurationError, WorkerConfigurationError
from worker.config import WorkerSettings

DSN = "postgresql+asyncpg://dmc:dmc@localhost:5432/dmc268"


def env(**overrides: str) -> dict[str, str]:
    return {"DATABASE_URL": DSN, **overrides}


def test_defaults_match_the_docs() -> None:
    settings = WorkerSettings.from_env(env())

    assert settings.worker_concurrency == 5
    assert settings.poll_interval_seconds == 5.0
    assert settings.reaper_interval_seconds == 30.0
    assert settings.db.database_url == DSN


def test_overrides_are_parsed() -> None:
    settings = WorkerSettings.from_env(
        env(
            WORKER_CONCURRENCY="2",
            POLL_INTERVAL_SECONDS="0.5",
            REAPER_INTERVAL_SECONDS="15",
        )
    )

    assert settings.worker_concurrency == 2
    assert settings.poll_interval_seconds == 0.5
    assert settings.reaper_interval_seconds == 15.0


@pytest.mark.parametrize("value", ["abc", "1.5"])
def test_a_non_integer_concurrency_is_an_error_naming_the_variable(value: str) -> None:
    with pytest.raises(WorkerConfigurationError, match="WORKER_CONCURRENCY"):
        WorkerSettings.from_env(env(WORKER_CONCURRENCY=value))


@pytest.mark.parametrize("value", ["0", "-3"])
def test_a_non_positive_concurrency_is_an_error_naming_the_variable(value: str) -> None:
    with pytest.raises(WorkerConfigurationError, match="WORKER_CONCURRENCY"):
        WorkerSettings.from_env(env(WORKER_CONCURRENCY=value))


@pytest.mark.parametrize("name", ["POLL_INTERVAL_SECONDS", "REAPER_INTERVAL_SECONDS"])
@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_bad_interval_values_are_errors_naming_their_variable(name: str, value: str) -> None:
    with pytest.raises(WorkerConfigurationError, match=name):
        WorkerSettings.from_env(env(**{name: value}))


@pytest.mark.parametrize("name", ["POLL_INTERVAL_SECONDS", "REAPER_INTERVAL_SECONDS"])
@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "infinity", "1e999"])
def test_non_finite_intervals_are_errors_naming_their_variable(name: str, value: str) -> None:
    # NaN would even pass the ``> 0`` check (every comparison with it is
    # False) and reach the loop as a poll interval.
    with pytest.raises(WorkerConfigurationError, match=name):
        WorkerSettings.from_env(env(**{name: value}))


def test_blank_values_mean_the_documented_defaults() -> None:
    # Same convention as adapters/db/config.py: a blank value is an unset
    # value, not a bad one.
    settings = WorkerSettings.from_env(env(WORKER_CONCURRENCY="   "))

    assert settings.worker_concurrency == 5


def test_a_missing_database_url_keeps_the_db_settings_error() -> None:
    # Composition: DATABASE_URL is validated exactly once, by DBSettings.
    with pytest.raises(DBConfigurationError, match="DATABASE_URL"):
        WorkerSettings.from_env({})


def test_the_repr_never_carries_the_dsn() -> None:
    settings = WorkerSettings.from_env(env())

    assert DSN not in repr(settings)
    assert DSN not in repr(settings.db)
