"""Repositories the review app can reach through an installation.

Read live from the forge and cached briefly: the backend design keeps forge state
out of the database, so there is no `repositories` table
(component-architecture-and-ER-model.md, "Deliberately absent").
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from domain.auth import Session
from domain.errors import InstallationGoneError
from domain.tenancy import AccountType, Organization

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from domain.ports import Clock, InstallationGateway, RepositoriesCache

PAGE_SIZE = 10

# github.com URL shapes. They live here because spec 002 Step 1 places connect_url
# in the domain; they are the one forge-specific detail outside adapters/, and the
# templates are isolated so moving them is a one-line change.
_ORGANIZATION_SETTINGS_URL = (
    "https://github.com/organizations/{login}/settings/installations/{installation_id}"
)
_USER_SETTINGS_URL = "https://github.com/settings/installations/{installation_id}"
_APP_INSTALL_URL = "https://github.com/apps/{slug}/installations/new"


@dataclass(frozen=True, slots=True)
class Repository:
    id: str
    owner: str
    name: str
    private: bool
    default_branch: str
    html_url: str
    # Null for every repository: GitHub does not record when one was added to an
    # installation, and we keep no copy. See spec 002 §3.
    connected_at: datetime | None = None
    # The newest review of this repository; null until review jobs exist.
    last_run_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.owner.strip() or not self.name.strip():
            raise ValueError("repository owner and name must not be empty")

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class RepositoryPage:
    items: tuple[Repository, ...]
    next_cursor: str | None
    total_count: int

    @classmethod
    def empty(cls) -> "RepositoryPage":
        return cls(items=(), next_cursor=None, total_count=0)


def sort_by_full_name(repositories: tuple[Repository, ...]) -> tuple[Repository, ...]:
    """Case-insensitive on "owner/name", matching the console's sortByFullName."""
    return tuple(sorted(repositories, key=lambda repository: repository.full_name.casefold()))


def page(
    repositories: tuple[Repository, ...],
    cursor: str | None = None,
    size: int = PAGE_SIZE,
) -> RepositoryPage:
    """One page of an already sorted list. The cursor is the offset as a string.

    Raises ValueError for a malformed or negative cursor; the API turns that into
    400 {"error": "bad_cursor"}.
    """
    offset = _parse_cursor(cursor)
    total = len(repositories)
    items = repositories[offset : offset + size]
    has_more = offset + size < total
    return RepositoryPage(
        items=items,
        next_cursor=str(offset + size) if has_more else None,
        total_count=total,
    )


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None or cursor == "":
        return 0
    # isascii() matters as well as isdigit(): "\u0661\u0660".isdigit() is True and
    # int() reads it as 10, so digits-only is not the same as the ASCII offset the
    # client can actually produce.
    if not (cursor.isascii() and cursor.isdigit()):
        raise ValueError(f"cursor must be a non-negative whole number, got {cursor!r}")
    return int(cursor)


def connect_url(organization: Organization | None, app_slug: str) -> str:
    """Where to send the user to connect a repository.

    With no organisation the app is installed nowhere, so the destination is its
    install page rather than an installation's settings.
    """
    if organization is None:
        return _APP_INSTALL_URL.format(slug=app_slug)
    return installation_settings_url(organization)


def installation_settings_url(organization: Organization) -> str:
    """Where repositories are added to and removed from this installation.

    GitHub has one such page per installation, not one per repository, so connect
    and disconnect both lead here. The per-repository disconnect endpoint exists
    for the 404, not for a different URL.
    """
    if organization.account_type is AccountType.USER:
        return _USER_SETTINGS_URL.format(installation_id=organization.installation_id)
    return _ORGANIZATION_SETTINGS_URL.format(
        login=organization.login, installation_id=organization.installation_id
    )


class RepositoriesService:
    """Reads the current organisation's repositories, cached for a short while."""

    def __init__(
        self,
        gateway: "InstallationGateway",
        cache: "RepositoriesCache",
        clock: "Clock",
        cache_ttl: timedelta,
    ) -> None:
        self._gateway = gateway
        self._cache = cache
        self._clock = clock
        self._cache_ttl = cache_ttl

    async def list_page(self, session: Session, cursor: str | None = None) -> RepositoryPage:
        return page(await self._all(session), cursor)

    async def find(self, session: Session, repository_id: str) -> Repository | None:
        """The repository with that id, or None when it is not connected.

        Answered from the same cached list the page uses, so it costs no extra
        forge call. The list can be up to one TTL stale, so a repository removed
        on GitHub moments ago may still be found: that only costs the user a trip
        to a settings page where it has already gone.
        """
        repositories = await self._all(session)
        return next((item for item in repositories if item.id == repository_id), None)

    async def _all(self, session: Session) -> tuple[Repository, ...]:
        """The whole sorted list. Empty when nothing is reachable, which covers
        both "installed nowhere" and "the installation is gone"."""
        organization = session.current_organization
        if organization is None:
            # Installed nowhere: the forge is not called at all.
            return ()

        installation_id = organization.installation_id
        cached = await self._cache.get(installation_id)
        if cached is not None:
            return cached

        try:
            fetched = await self._gateway.list_repositories(installation_id)
        except InstallationGoneError:
            # Uninstalled or suspended. The console shows "no repositories" rather
            # than an error, and the next sign-in refreshes the organisations.
            await self._cache.drop(installation_id)
            return ()

        sorted_repositories = sort_by_full_name(fetched)
        await self._cache.put(
            installation_id, sorted_repositories, self._clock.now() + self._cache_ttl
        )
        return sorted_repositories

    async def forget(self, session: Session | None) -> None:
        """Drop the cache for every installation the session can see."""
        if session is None:
            return
        for organization in session.organizations:
            await self._cache.drop(organization.installation_id)
