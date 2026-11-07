import random

import pytest

from domain.resilience import CircuitBreaker, RetryPolicy


def test_delay_grows_and_gives_up_at_max_attempts() -> None:
    policy = RetryPolicy(max_attempts=3, base_seconds=1, cap_seconds=8, jitter_fraction=0)
    rng = random.Random(0)
    assert policy.delay(0, rng) == 1
    assert policy.delay(1, rng) == 2
    assert policy.delay(2, rng) is None


def test_delay_is_capped_and_jittered() -> None:
    policy = RetryPolicy(max_attempts=10, base_seconds=1, cap_seconds=4, jitter_fraction=0.2)
    rng = random.Random(1)
    for attempt in range(6):
        delay = policy.delay(attempt, rng)
        assert delay is not None
        assert delay <= 4 * 1.2


def test_retry_after_wins_when_larger_and_long_ones_are_not_waited_out() -> None:
    policy = RetryPolicy(max_attempts=3, base_seconds=1, cap_seconds=8, jitter_fraction=0)
    rng = random.Random(0)
    assert policy.delay(0, rng, retry_after=5) == 5
    assert policy.delay(0, rng, retry_after=0.1) == 1
    assert policy.delay(0, rng, retry_after=60) is None


def test_policy_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_seconds=5, cap_seconds=1)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_breaker_opens_after_threshold_then_probes_and_closes() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=10, clock=clock)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "open"
    assert not breaker.allow()
    assert breaker.retry_in() == 10
    clock.now = 10
    assert breaker.state == "half_open"
    assert breaker.allow()
    assert not breaker.allow()  # only one probe at a time
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow()


def test_failed_probe_reopens() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=5, clock=clock)
    breaker.record_failure()
    clock.now = 5
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.retry_in() == 5


def test_success_resets_the_failure_count() -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=5, clock=_Clock())
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state == "closed"


def test_releasing_the_probe_lets_the_next_call_probe() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=5, clock=clock)
    breaker.record_failure()
    clock.now = 5
    assert breaker.allow()
    breaker.release_probe()
    assert breaker.allow()
