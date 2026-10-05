"""Conformance suite for SessionStore. The PostgreSQL adapter must pass it too."""

from datetime import timedelta

import pytest

from adapters.memory.auth import InMemorySessionStore
from domain.ports import SessionStore
from tests.conftest import FixedClock, make_session


@pytest.fixture
def store(auth_clock: FixedClock) -> SessionStore:
    return InMemorySessionStore(auth_clock)


async def test_get_returns_what_create_stored(store: SessionStore, auth_clock: FixedClock) -> None:
    session = make_session(auth_clock, id_hash="h1")
    await store.create(session)

    assert await store.get("h1") == session


async def test_get_is_none_for_an_unknown_hash(store: SessionStore) -> None:
    assert await store.get("missing") is None


async def test_save_replaces_the_stored_session(
    store: SessionStore, auth_clock: FixedClock
) -> None:
    session = make_session(auth_clock, id_hash="h1")
    await store.create(session)
    auth_clock.advance(10)

    refreshed = session.touch(auth_clock.now(), timedelta(seconds=3600))
    await store.save(refreshed)

    assert await store.get("h1") == refreshed


async def test_delete_removes_the_session(store: SessionStore, auth_clock: FixedClock) -> None:
    await store.create(make_session(auth_clock, id_hash="h1"))

    await store.delete("h1")

    assert await store.get("h1") is None


async def test_delete_of_an_unknown_hash_is_silent(store: SessionStore) -> None:
    await store.delete("missing")


async def test_an_expired_session_is_ignored_on_read(
    store: SessionStore, auth_clock: FixedClock
) -> None:
    await store.create(make_session(auth_clock, id_hash="h1", ttl_seconds=60))

    auth_clock.advance(60)

    assert await store.get("h1") is None
