"""domain.jobs: backoff formula bounds and the D5 storage clamp.

PIPELINE_SPEC §4.3 fixes the formula; the tests pin the cap and the ±20%
jitter window without sleeping (the rng is injected).
"""

import random
from datetime import timedelta

import pytest

from domain.jobs import (
    DEFAULT_MAX_RETRIES,
    LEASE_SECONDS,
    MAX_IN_FLIGHT_PER_INSTALLATION,
    backoff_delay,
    clamp_for_storage,
)


class FixedJitter:
    """Stands in for random.Random: uniform always returns `factor`."""

    def __init__(self, factor: float) -> None:
        self.factor = factor

    def uniform(self, low: float, high: float) -> float:
        return self.factor


# --- constants ------------------------------------------------------------


def test_the_constants_match_the_documents() -> None:
    assert MAX_IN_FLIGHT_PER_INSTALLATION == 3
    assert DEFAULT_MAX_RETRIES == 3
    assert LEASE_SECONDS == 600


# --- backoff --------------------------------------------------------------


@pytest.mark.parametrize(
    ("retry_count", "expected_seconds"),
    [(0, 60), (1, 120), (2, 240), (3, 480), (4, 900), (5, 900), (6, 900)],
)
def test_backoff_base_doubles_then_caps_at_fifteen_minutes(
    retry_count: int, expected_seconds: int
) -> None:
    # factor 0 → multiplier exactly 1.0, so the base shows through.
    assert backoff_delay(retry_count, FixedJitter(0.0)) == timedelta(seconds=expected_seconds)


@pytest.mark.parametrize("retry_count", [0, 1, 2, 3, 4, 6])
def test_backoff_jitter_stays_within_twenty_percent(retry_count: int) -> None:
    rng = random.Random(20261010)
    base = min(timedelta(seconds=60) * 2**retry_count, timedelta(minutes=15))

    delay = backoff_delay(retry_count, rng)

    assert timedelta(seconds=base.total_seconds() * 0.8) <= delay
    assert delay <= timedelta(seconds=base.total_seconds() * 1.2)


def test_backoff_jitter_extends_to_the_upper_bound() -> None:
    assert backoff_delay(2, FixedJitter(0.2)) == timedelta(seconds=240 * 1.2)


def test_backoff_rejects_a_negative_retry_count() -> None:
    with pytest.raises(ValueError, match="retry_count"):
        backoff_delay(-1, random.Random(0))


# --- clamp ----------------------------------------------------------------


def test_clamp_leaves_short_values_untouched() -> None:
    assert clamp_for_storage("Add payments webhook") == "Add payments webhook"


def test_clamp_is_identity_at_the_limit() -> None:
    value = "t" * 255
    assert clamp_for_storage(value) == value


def test_clamp_truncates_past_the_default_limit() -> None:
    value = "t" * 300
    assert clamp_for_storage(value) == "t" * 255
    assert len(clamp_for_storage(value)) == 255


def test_clamp_honours_an_explicit_limit() -> None:
    assert clamp_for_storage("abcdef", limit=3) == "abc"
