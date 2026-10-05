"""The service against fake ports: no network, no clock reads but the injected one."""

from datetime import timedelta

import pytest

from adapters.memory.repositories import InMemoryRepositoriesCache
from domain.errors import ForgeUnavailableError, InstallationGoneError
from domain.repositories import RepositoriesService
from tests.conftest import (
    FakeInstallationGateway,
    FixedClock,
    make_organization,
    make_repository,
    make_session,
)

ORG = make_organization("acme", "100", installation_id=42)


def service(
    clock: FixedClock,
    gateway: FakeInstallationGateway,
    cache: InMemoryRepositoriesCache,
    ttl_seconds: int = 60,
) -> RepositoriesService:
    return RepositoriesService(
        gateway=gateway, cache=cache, clock=clock, cache_ttl=timedelta(seconds=ttl_seconds)
    )


async def test_no_organisation_is_an_empty_page_and_github_is_not_called(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway((make_repository(),))
    session = make_session(auth_clock, organizations=())

    result = await service(auth_clock, gateway, repositories_cache).list_page(session)

    assert result.items == ()
    assert result.next_cursor is None
    assert result.total_count == 0
    assert gateway.calls == []


async def test_the_list_is_fetched_sorted_and_paged(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway(
        (make_repository("acme", "web"), make_repository("acme", "api"))
    )
    session = make_session(auth_clock, organizations=(ORG,))

    result = await service(auth_clock, gateway, repositories_cache).list_page(session)

    assert [item.name for item in result.items] == ["api", "web"]
    assert gateway.calls == [42]


async def test_a_second_call_within_the_ttl_does_not_reach_github(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway((make_repository(),))
    session = make_session(auth_clock, organizations=(ORG,))
    subject = service(auth_clock, gateway, repositories_cache)

    await subject.list_page(session)
    auth_clock.advance(59)
    await subject.list_page(session)

    assert gateway.calls == [42]


async def test_the_cache_expires(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway((make_repository(),))
    session = make_session(auth_clock, organizations=(ORG,))
    subject = service(auth_clock, gateway, repositories_cache)

    await subject.list_page(session)
    auth_clock.advance(60)
    await subject.list_page(session)

    assert gateway.calls == [42, 42]


async def test_forget_drops_the_cache_so_the_next_call_refetches(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway((make_repository(),))
    session = make_session(auth_clock, organizations=(ORG,))
    subject = service(auth_clock, gateway, repositories_cache)

    await subject.list_page(session)
    await subject.forget(session)
    await subject.list_page(session)

    assert gateway.calls == [42, 42]


async def test_forget_without_a_session_is_silent(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    await service(auth_clock, FakeInstallationGateway(), repositories_cache).forget(None)


async def test_an_installation_that_is_gone_is_an_empty_page_not_an_error(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway(error=InstallationGoneError(42, "HTTP 404"))
    session = make_session(auth_clock, organizations=(ORG,))

    result = await service(auth_clock, gateway, repositories_cache).list_page(session)

    assert result.items == ()
    assert result.total_count == 0
    assert len(repositories_cache) == 0


async def test_a_forge_failure_propagates(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway(error=ForgeUnavailableError("github", "HTTP 503"))
    session = make_session(auth_clock, organizations=(ORG,))

    with pytest.raises(ForgeUnavailableError):
        await service(auth_clock, gateway, repositories_cache).list_page(session)


async def test_a_bad_cursor_raises_for_the_api_to_turn_into_400(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway((make_repository(),))
    session = make_session(auth_clock, organizations=(ORG,))

    with pytest.raises(ValueError, match="cursor must be"):
        await service(auth_clock, gateway, repositories_cache).list_page(session, "-1")


# --- find ---------------------------------------------------------------


async def test_find_returns_a_connected_repository(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    wanted = make_repository("acme", "payments")
    gateway = FakeInstallationGateway((make_repository("acme", "web"), wanted))
    session = make_session(auth_clock, organizations=(ORG,))

    found = await service(auth_clock, gateway, repositories_cache).find(session, wanted.id)

    assert found == wanted


async def test_find_is_none_for_an_unknown_id(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway((make_repository(),))
    session = make_session(auth_clock, organizations=(ORG,))

    assert await service(auth_clock, gateway, repositories_cache).find(session, "nope") is None


async def test_find_is_none_without_an_organisation(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway((make_repository(),))
    session = make_session(auth_clock, organizations=())

    found = await service(auth_clock, gateway, repositories_cache).find(session, "acme-payments")

    assert found is None
    assert gateway.calls == []


async def test_find_is_none_when_the_installation_is_gone(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    gateway = FakeInstallationGateway(error=InstallationGoneError(42, "HTTP 404"))
    session = make_session(auth_clock, organizations=(ORG,))

    assert await service(auth_clock, gateway, repositories_cache).find(session, "x") is None


async def test_find_shares_the_cache_with_the_listing(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    """It must cost no extra forge call."""
    gateway = FakeInstallationGateway((make_repository(),))
    session = make_session(auth_clock, organizations=(ORG,))
    subject = service(auth_clock, gateway, repositories_cache)

    await subject.list_page(session)
    await subject.find(session, "acme-payments")

    assert gateway.calls == [42]


async def test_find_sees_past_the_first_page(
    auth_clock: FixedClock, repositories_cache: InMemoryRepositoriesCache
) -> None:
    """A repository on page three is still connected."""
    many = tuple(make_repository("acme", f"repo-{index:02d}") for index in range(25))
    gateway = FakeInstallationGateway(many)
    session = make_session(auth_clock, organizations=(ORG,))

    found = await service(auth_clock, gateway, repositories_cache).find(session, "acme-repo-24")

    assert found is not None
    assert found.name == "repo-24"
