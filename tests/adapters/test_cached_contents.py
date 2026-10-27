"""The TTL cache over `RepositoryContents` (review-context spec)."""

from datetime import datetime, timedelta

import pytest

from adapters.memory.cached_contents import CachedRepositoryContents
from domain.errors import ForgeUnavailableError
from domain.ports import Clock


class FakeClock(Clock):
    def __init__(self) -> None:
        self.now_value = datetime(2026, 10, 27, 12, 0, 0)

    def now(self) -> datetime:
        return self.now_value

    def advance(self, seconds: float) -> None:
        self.now_value += timedelta(seconds=seconds)


class CountingContents:
    """A fake port that counts calls and answers from a scripted map."""

    def __init__(self, files: dict[tuple[str, str], bytes | None]) -> None:
        self.files = files
        self.calls: list[tuple[int, str, str, str, int]] = []

    async def read_file(
        self, installation_id: int, repo_full_name: str, path: str, ref: str, *, max_bytes: int
    ) -> bytes | None:
        self.calls.append((installation_id, repo_full_name, path, ref, max_bytes))
        if ("boom", path) in self.files and self.files[("boom", path)] == b"raise":
            raise ForgeUnavailableError("github", "contents fetch failed: network error")
        return self.files.get((ref, path))


RULES = b"# Rules\n- be kind\n"
SHA = "3f2a9c1" * 5  # any stable ref string


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_cache(inner: CountingContents, clock: FakeClock) -> CachedRepositoryContents:
    return CachedRepositoryContents(inner, clock, ttl_seconds=600.0)


async def test_second_read_within_ttl_hits_the_cache(clock: FakeClock) -> None:
    inner = CountingContents({(SHA, ".review/rules.md"): RULES})
    cache = make_cache(inner, clock)

    first = await cache.read_file(1, "o/r", ".review/rules.md", SHA, max_bytes=65_537)
    second = await cache.read_file(1, "o/r", ".review/rules.md", SHA, max_bytes=65_537)

    assert first == second == RULES
    assert len(inner.calls) == 1


async def test_expired_entry_is_fetched_again(clock: FakeClock) -> None:
    inner = CountingContents({(SHA, ".review/rules.md"): RULES})
    cache = make_cache(inner, clock)

    await cache.read_file(1, "o/r", ".review/rules.md", SHA, max_bytes=65_537)
    clock.advance(601)
    await cache.read_file(1, "o/r", ".review/rules.md", SHA, max_bytes=65_537)

    assert len(inner.calls) == 2


async def test_absent_file_is_cached_too(clock: FakeClock) -> None:
    inner = CountingContents({})
    cache = make_cache(inner, clock)

    assert await cache.read_file(1, "o/r", ".review/rules.md", SHA, max_bytes=65_537) is None
    assert await cache.read_file(1, "o/r", ".review/rules.md", SHA, max_bytes=65_537) is None
    assert len(inner.calls) == 1


async def test_errors_are_not_cached(clock: FakeClock) -> None:
    inner = CountingContents({("boom", ".review/rules.md"): b"raise"})
    cache = make_cache(inner, clock)

    with pytest.raises(ForgeUnavailableError):
        await cache.read_file(1, "o/r", ".review/rules.md", "boom", max_bytes=65_537)
    assert len(cache) == 0


async def test_different_key_dimensions_do_not_collide(clock: FakeClock) -> None:
    inner = CountingContents({(SHA, ".review/rules.md"): RULES})
    cache = make_cache(inner, clock)

    await cache.read_file(1, "o/r", ".review/rules.md", SHA, max_bytes=65_537)
    await cache.read_file(2, "o/r", ".review/rules.md", SHA, max_bytes=65_537)
    await cache.read_file(1, "o/r", ".review/rules.md", "other-ref", max_bytes=65_537)
    await cache.read_file(1, "o/r", ".review/rules.md", SHA, max_bytes=1000)

    assert len(inner.calls) == 4
