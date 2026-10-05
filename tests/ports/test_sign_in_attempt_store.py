"""Conformance suite for SignInAttemptStore. The PostgreSQL adapter must pass it too."""

import pytest

from adapters.memory.auth import InMemorySignInAttemptStore
from domain.ports import SignInAttemptStore
from tests.conftest import FixedClock, make_attempt


@pytest.fixture
def store(auth_clock: FixedClock) -> SignInAttemptStore:
    return InMemorySignInAttemptStore(auth_clock)


async def test_take_returns_what_put_stored(
    store: SignInAttemptStore, auth_clock: FixedClock
) -> None:
    attempt = make_attempt(auth_clock, attempt_id="a1")
    await store.put(attempt)

    assert await store.take("a1") == attempt


async def test_take_is_single_use(store: SignInAttemptStore, auth_clock: FixedClock) -> None:
    await store.put(make_attempt(auth_clock, attempt_id="a1"))

    assert await store.take("a1") is not None
    assert await store.take("a1") is None


async def test_take_is_none_for_an_unknown_id(store: SignInAttemptStore) -> None:
    assert await store.take("missing") is None


async def test_an_expired_attempt_is_ignored_on_read(
    store: SignInAttemptStore, auth_clock: FixedClock
) -> None:
    await store.put(make_attempt(auth_clock, attempt_id="a1", ttl_seconds=600))

    auth_clock.advance(600)

    assert await store.take("a1") is None


async def test_expired_entries_are_purged_on_write(
    store: SignInAttemptStore, auth_clock: FixedClock
) -> None:
    await store.put(make_attempt(auth_clock, attempt_id="stale", ttl_seconds=60))
    auth_clock.advance(60)

    await store.put(make_attempt(auth_clock, attempt_id="fresh"))

    assert await store.take("stale") is None
    assert await store.take("fresh") is not None
