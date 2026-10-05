"""In-memory session and sign-in-attempt stores.

Stand-ins until the database exists. `adapters/db/auth.py` replaces them behind
the same ports, and must pass the same conformance suite.
"""

from datetime import datetime

from domain.auth import Session, SignInAttempt
from domain.ports import Clock


class InMemorySessionStore:
    """Sessions keyed by sha256(cookie value); the raw value never arrives here."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    def __len__(self) -> int:
        """How many sessions are held, expired ones included. For tests and
        diagnostics; not part of the SessionStore port."""
        return len(self._sessions)

    async def create(self, session: Session) -> None:
        self._purge(self._clock.now())
        self._sessions[session.id_hash] = session

    async def get(self, id_hash: str) -> Session | None:
        session = self._sessions.get(id_hash)
        if session is None:
            return None
        if session.is_expired(self._clock.now()):
            # Ignored on read, and dropped so it cannot accumulate.
            del self._sessions[id_hash]
            return None
        return session

    async def save(self, session: Session) -> None:
        self._sessions[session.id_hash] = session

    async def delete(self, id_hash: str) -> None:
        self._sessions.pop(id_hash, None)

    def _purge(self, now: datetime) -> None:
        expired = [
            id_hash for id_hash, session in self._sessions.items() if session.is_expired(now)
        ]
        for id_hash in expired:
            del self._sessions[id_hash]


class InMemorySignInAttemptStore:
    """Single-use sign-in attempts: a replayed state finds nothing."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._attempts: dict[str, SignInAttempt] = {}

    async def put(self, attempt: SignInAttempt) -> None:
        self._purge(self._clock.now())
        self._attempts[attempt.attempt_id] = attempt

    async def take(self, attempt_id: str) -> SignInAttempt | None:
        attempt = self._attempts.pop(attempt_id, None)
        if attempt is None:
            return None
        if attempt.is_expired(self._clock.now()):
            return None
        return attempt

    def _purge(self, now: datetime) -> None:
        expired = [
            attempt_id for attempt_id, attempt in self._attempts.items() if attempt.is_expired(now)
        ]
        for attempt_id in expired:
            del self._attempts[attempt_id]
