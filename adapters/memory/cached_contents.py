"""Caching decorator over the `RepositoryContents` port (review-context spec).

Keyed by (installation, repository, ref, path, max_bytes) with an absolute
expiry, like `InMemoryRepositoriesCache`. A ref that is an immutable SHA makes
an entry factually stable — the TTL only bounds memory. "File absent" (None) is
a stable answer on an immutable ref and is cached like any other.

The cached bytes are untrusted repository content (rules text, source files):
they live in this process's memory only, are never persisted anywhere and never
logged (AGENTS.md hard rules 2-3). Errors from the inner port are not cached.
"""

from datetime import datetime, timedelta

from domain.ports import Clock, RepositoryContents

#: How long a fetched file is kept. Facts at an immutable SHA do not go stale;
#: this only bounds the memory the cache may hold.
DEFAULT_TTL_SECONDS = 600.0

#: Entries held at most; the oldest inserted are evicted first.
DEFAULT_MAX_ENTRIES = 256


class CachedRepositoryContents:
    """Implements `RepositoryContents` over another one, in memory."""

    def __init__(
        self,
        inner: RepositoryContents,
        clock: Clock,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._inner = inner
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_entries = max_entries
        # Insertion-ordered: the first key is the oldest entry, evicted first.
        self._entries: dict[tuple[int, str, str, str, int], tuple[bytes | None, datetime]] = {}

    def __len__(self) -> int:
        """Entries held, expired ones included. For tests, not part of the port."""
        return len(self._entries)

    async def read_file(
        self,
        installation_id: int,
        repo_full_name: str,
        path: str,
        ref: str,
        *,
        max_bytes: int,
    ) -> bytes | None:
        key = (installation_id, repo_full_name, ref, path, max_bytes)
        entry = self._entries.get(key)
        if entry is not None:
            value, expires_at = entry
            if self._clock.now() < expires_at:
                return value
            del self._entries[key]
        value = await self._inner.read_file(
            installation_id, repo_full_name, path, ref, max_bytes=max_bytes
        )
        self._entries[key] = (value, self._clock.now() + self._ttl)
        while len(self._entries) > self._max_entries:
            oldest = next(iter(self._entries))
            del self._entries[oldest]
        return value
