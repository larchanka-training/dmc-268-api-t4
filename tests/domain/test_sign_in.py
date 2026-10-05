import pytest

from domain.auth import (
    CODE_VERIFIER_MAX_LENGTH,
    CODE_VERIFIER_MIN_LENGTH,
    SignInAttempt,
    SignInResult,
)
from tests.conftest import FixedClock, make_attempt


def test_the_four_outcomes_are_the_agreed_wire_values() -> None:
    assert {result.value for result in SignInResult} == {
        "success",
        "access_denied",
        "state_mismatch",
        "server_error",
    }


def test_attempt_matches_only_its_own_state() -> None:
    attempt = make_attempt(FixedClock(), state="the-state")

    assert attempt.matches("the-state")
    assert not attempt.matches("other")
    assert not attempt.matches("")


def test_attempt_expires_at_its_ttl() -> None:
    clock = FixedClock()
    attempt = make_attempt(clock, ttl_seconds=600)

    assert not attempt.is_expired(clock.now())
    clock.advance(600)
    assert attempt.is_expired(clock.now())


@pytest.mark.parametrize("length", [CODE_VERIFIER_MIN_LENGTH, CODE_VERIFIER_MAX_LENGTH])
def test_code_verifier_length_bounds_are_accepted(length: int) -> None:
    SignInAttempt(attempt_id="a", state="s", code_verifier="v" * length)


@pytest.mark.parametrize("length", [CODE_VERIFIER_MIN_LENGTH - 1, CODE_VERIFIER_MAX_LENGTH + 1])
def test_code_verifier_outside_the_bounds_is_refused(length: int) -> None:
    with pytest.raises(ValueError, match="code_verifier length"):
        SignInAttempt(attempt_id="a", state="s", code_verifier="v" * length)


def test_code_verifier_is_not_in_the_repr() -> None:
    attempt = make_attempt(FixedClock())

    assert "v" * 64 not in repr(attempt)
