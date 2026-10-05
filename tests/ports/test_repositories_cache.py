"""Conformance suite for RepositoriesCache. A Redis or SQL cache must pass it too."""

from datetime import timedelta

import pytest

from adapters.memory.repositories import InMemoryRepositoriesCache
from domain.ports import RepositoriesCache
from tests.conftest import FixedClock, make_repository

REPOS = (make_repository("acme", "api"),)


@pytest.fixture
def cache(auth_clock: FixedClock) -> RepositoriesCache:
    return InMemoryRepositoriesCache(auth_clock)


def later(clock: FixedClock, seconds: int = 60) -> object:
    return clock.now() + timedelta(seconds=seconds)


async def test_get_returns_what_put_stored(
    cache: RepositoriesCache, auth_clock: FixedClock
) -> None:
    await cache.put(42, REPOS, later(auth_clock))  # type: ignore[arg-type]

    assert await cache.get(42) == REPOS


async def test_get_is_none_for_an_unknown_installation(cache: RepositoriesCache) -> None:
    assert await cache.get(99) is None


async def test_put_overwrites(cache: RepositoriesCache, auth_clock: FixedClock) -> None:
    replacement = (make_repository("acme", "web"),)
    await cache.put(42, REPOS, later(auth_clock))  # type: ignore[arg-type]

    await cache.put(42, replacement, later(auth_clock))  # type: ignore[arg-type]

    assert await cache.get(42) == replacement


async def test_an_expired_entry_is_ignored_on_read(
    cache: RepositoriesCache, auth_clock: FixedClock
) -> None:
    await cache.put(42, REPOS, later(auth_clock, 60))  # type: ignore[arg-type]

    auth_clock.advance(60)

    assert await cache.get(42) is None


async def test_drop_removes_the_entry(cache: RepositoriesCache, auth_clock: FixedClock) -> None:
    await cache.put(42, REPOS, later(auth_clock))  # type: ignore[arg-type]

    await cache.drop(42)

    assert await cache.get(42) is None


async def test_drop_of_an_unknown_installation_is_silent(cache: RepositoriesCache) -> None:
    await cache.drop(99)


async def test_expired_entries_are_purged_on_write(
    cache: RepositoriesCache, auth_clock: FixedClock
) -> None:
    await cache.put(1, REPOS, later(auth_clock, 30))  # type: ignore[arg-type]
    auth_clock.advance(30)

    await cache.put(2, REPOS, later(auth_clock, 60))  # type: ignore[arg-type]

    assert len(cache) == 1  # type: ignore[arg-type]


async def test_entries_are_kept_per_installation(
    cache: RepositoriesCache, auth_clock: FixedClock
) -> None:
    other = (make_repository("beta", "app"),)
    await cache.put(1, REPOS, later(auth_clock))  # type: ignore[arg-type]
    await cache.put(2, other, later(auth_clock))  # type: ignore[arg-type]

    assert await cache.get(1) == REPOS
    assert await cache.get(2) == other
