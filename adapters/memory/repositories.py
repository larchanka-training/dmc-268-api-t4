"""In-memory repositories cache.

A stand-in behind `RepositoriesCache`: a Redis or PostgreSQL cache replaces it
later and must pass the same conformance suite.
"""

from datetime import datetime

from domain.ports import Clock
from domain.repositories import Repository


class InMemoryRepositoriesCache:
    """Keyed by installation. Entries carry an absolute expiry."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._entries: dict[int, tuple[tuple[Repository, ...], datetime]] = {}

    def __len__(self) -> int:
        """Entries held, expired ones included. For tests, not part of the port."""
        return len(self._entries)

    async def get(self, installation_id: int) -> tuple[Repository, ...] | None:
        entry = self._entries.get(installation_id)
        if entry is None:
            return None
        repositories, expires_at = entry
        if self._clock.now() >= expires_at:
            del self._entries[installation_id]
            return None
        return repositories

    async def put(
        self,
        installation_id: int,
        repositories: tuple[Repository, ...],
        expires_at: datetime,
    ) -> None:
        self._purge()
        self._entries[installation_id] = (repositories, expires_at)

    async def drop(self, installation_id: int) -> None:
        self._entries.pop(installation_id, None)

    def _purge(self) -> None:
        now = self._clock.now()
        expired = [key for key, (_, expires_at) in self._entries.items() if now >= expires_at]
        for key in expired:
            del self._entries[key]
