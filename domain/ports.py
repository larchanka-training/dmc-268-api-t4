from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from domain.auth import Session, SignInAttempt
from domain.models import ReviewResult
from domain.repositories import Repository
from domain.tenancy import Organization, User


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    diff_text: str
    pr_title: str
    pr_description: str


class LLMGateway(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewResult: ...


@dataclass(frozen=True, slots=True)
class Token:
    """A user-to-server token. Used once, at sign-in, then discarded.

    repr is suppressed on every secret so a traceback or a log line that
    formats this object cannot leak it.
    """

    access_token: str = field(repr=False)
    token_type: str = "bearer"
    expires_in: int | None = None
    refresh_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Profile:
    """Who signed in, and which accounts they can reach, already mapped to the domain."""

    user: User
    organizations: tuple[Organization, ...]


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdentityProvider(Protocol):
    def authorize_url(self, state: str, code_challenge: str) -> str: ...

    async def exchange_code(self, code: str, code_verifier: str) -> Token: ...

    async def load_profile(self, token: Token) -> Profile: ...


class SessionStore(Protocol):
    async def create(self, session: Session) -> None: ...

    async def get(self, id_hash: str) -> Session | None: ...

    async def save(self, session: Session) -> None: ...

    async def delete(self, id_hash: str) -> None: ...


class SignInAttemptStore(Protocol):
    async def put(self, attempt: SignInAttempt) -> None: ...

    async def take(self, attempt_id: str) -> SignInAttempt | None:
        """Single use: a replayed attempt id finds nothing."""
        ...


class InstallationGateway(Protocol):
    """Reads what an installation can see on the forge."""

    async def list_repositories(self, installation_id: int) -> tuple[Repository, ...]: ...


class RepositoriesCache(Protocol):
    """A short-lived cache, keyed by installation.

    The repository list is forge state, so it is read live and cached rather than
    stored; `expires_at` is absolute so the cache needs no clock of its own.
    """

    async def get(self, installation_id: int) -> tuple[Repository, ...] | None: ...

    async def put(
        self,
        installation_id: int,
        repositories: tuple[Repository, ...],
        expires_at: datetime,
    ) -> None: ...

    async def drop(self, installation_id: int) -> None: ...
