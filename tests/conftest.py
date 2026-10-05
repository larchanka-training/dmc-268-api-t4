import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from adapters.llm.keys import KeyPool
from adapters.llm.openai_compat import OpenAICompatibleProvider
from adapters.memory.auth import InMemorySessionStore, InMemorySignInAttemptStore
from adapters.memory.repositories import InMemoryRepositoriesCache
from api.config import AppSettings
from api.main import create_app
from domain.auth import Session, SignInAttempt
from domain.ports import Profile, Token
from domain.repositories import RepositoriesService, Repository
from domain.tenancy import AccountType, Organization, OrganizationRole, User

FAKE_KEY_A = "sk-test-aaaaaaaaaaaaaaaa1111"
FAKE_KEY_B = "sk-test-bbbbbbbbbbbbbbbb2222"
FAKE_KEY_C = "sk-test-cccccccccccccccc3333"

VALID_FINDING: dict[str, Any] = {
    "path": "app/users.py",
    "line": 16,
    "side": "new",
    "severity": "critical",
    "category": "security",
    "message": "The sort parameter is interpolated into SQL; whitelist the column names.",
    "suggestion": {
        "before": ['    rows = conn.execute(f"SELECT ... ORDER BY {sort}").fetchall()'],
        "after": [
            '    rows = conn.execute(f"SELECT ... ORDER BY {SORT_COLUMNS[sort]}").fetchall()'
        ],
    },
}


def review_json(*findings: dict[str, Any], summary: str = "Adds a users endpoint.") -> str:
    return json.dumps({"summary": summary, "findings": list(findings)})


def chat_response(content: str | None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


Handler = Callable[[httpx.Request], httpx.Response]


class RecordingTransport(httpx.MockTransport):
    """MockTransport that keeps every request it served."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []

        def recording_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(recording_handler)


def replies(*responses: httpx.Response | Exception) -> Handler:
    """Serve the given responses in order; an exception instance is raised instead."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


def make_provider(
    transport: httpx.AsyncBaseTransport,
    *,
    name: str = "eurouter",
    keys: tuple[str, ...] = (FAKE_KEY_A,),
    clock: FakeClock | None = None,
    json_mode: bool = True,
    provider_order: tuple[str, ...] = (),
) -> OpenAICompatibleProvider:
    pool = KeyPool(name, keys, clock or FakeClock()) if keys else None
    return OpenAICompatibleProvider(
        name=name,
        base_url="https://llm.example.test/api/v1",
        model=f"{name}-model",
        key_pool=pool,
        temperature=0.0,
        json_mode=json_mode,
        timeout_seconds=5.0,
        max_tokens=1024,
        provider_order=provider_order,
        transport=transport,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# --- Sign-in helpers (spec 001) ------------------------------------------

GITHUB_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github"
TEST_ORIGIN = "https://testserver"
FAKE_CODE = "gho-test-code-aaaa"
FAKE_TOKEN = "ghu_test_aaaaaaaaaaaaaaaaaaaa"


def github_fixture(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((GITHUB_FIXTURES / f"{name}.json").read_text("utf-8"))
    return body


class FixedClock:
    """A datetime clock for the auth flow. Advance it instead of sleeping."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def make_user(login: str = "octocat", identifier: str = "1") -> User:
    return User(
        id=identifier,
        login=login,
        name="Octo Cat",
        avatar_url="https://avatars.githubusercontent.com/u/1",
    )


def make_organization(
    login: str = "acme",
    identifier: str = "100",
    role: OrganizationRole = OrganizationRole.OWNER,
    *,
    installation_id: int = 10,
    account_type: AccountType = AccountType.ORGANIZATION,
) -> Organization:
    return Organization(
        id=identifier,
        login=login,
        name=login.title(),
        avatar_url=f"https://avatars.githubusercontent.com/u/{identifier}",
        role=role,
        installation_id=installation_id,
        account_type=account_type,
    )


def make_session(
    clock: FixedClock,
    *,
    id_hash: str = "hash",
    organizations: tuple[Organization, ...] = (),
    ttl_seconds: int = 3600,
) -> Session:
    return Session.issue(
        id_hash=id_hash,
        user=make_user(),
        organizations=organizations,
        now=clock.now(),
        ttl=timedelta(seconds=ttl_seconds),
    )


def make_attempt(
    clock: FixedClock,
    *,
    attempt_id: str = "attempt-1",
    state: str = "state-1",
    return_to: str | None = None,
    ttl_seconds: int = 600,
) -> SignInAttempt:
    return SignInAttempt(
        attempt_id=attempt_id,
        state=state,
        code_verifier="v" * 64,
        return_to=return_to,
        expires_at=clock.now() + timedelta(seconds=ttl_seconds),
    )


class FakeIdentityProvider:
    """Records calls; never touches the network."""

    def __init__(
        self,
        profile: Profile | None = None,
        *,
        exchange_error: Exception | None = None,
        profile_error: Exception | None = None,
    ) -> None:
        self.profile = profile or Profile(user=make_user(), organizations=(make_organization(),))
        self.exchange_error = exchange_error
        self.profile_error = profile_error
        self.authorize_calls: list[tuple[str, str]] = []
        self.exchange_calls: list[tuple[str, str]] = []

    def authorize_url(self, state: str, code_challenge: str) -> str:
        self.authorize_calls.append((state, code_challenge))
        return f"https://github.example.test/login/oauth/authorize?state={state}"

    async def exchange_code(self, code: str, code_verifier: str) -> Token:
        self.exchange_calls.append((code, code_verifier))
        if self.exchange_error is not None:
            raise self.exchange_error
        return Token(access_token=FAKE_TOKEN)

    async def load_profile(self, token: Token) -> Profile:
        if self.profile_error is not None:
            raise self.profile_error
        return self.profile


def make_settings(session_ttl_seconds: int = 3600, state_ttl_seconds: int = 600) -> AppSettings:
    return AppSettings(
        app_origin=TEST_ORIGIN,
        session_ttl_seconds=session_ttl_seconds,
        oauth_state_ttl_seconds=state_ttl_seconds,
    )


APP_SLUG = "review-agent"


def make_repository(
    owner: str = "acme",
    name: str = "payments",
    *,
    private: bool = True,
    default_branch: str = "main",
    last_run_at: datetime | None = None,
) -> Repository:
    return Repository(
        id=f"{owner}-{name}",
        owner=owner,
        name=name,
        private=private,
        default_branch=default_branch,
        html_url=f"https://github.com/{owner}/{name}",
        connected_at=None,
        last_run_at=last_run_at,
    )


class FakeInstallationGateway:
    """Records which installations were asked for; never touches the network."""

    def __init__(
        self,
        repositories: tuple[Repository, ...] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self.repositories = repositories
        self.error = error
        self.calls: list[int] = []

    async def list_repositories(self, installation_id: int) -> tuple[Repository, ...]:
        self.calls.append(installation_id)
        if self.error is not None:
            raise self.error
        return self.repositories


def build_test_app(
    clock: FixedClock,
    identity: "FakeIdentityProvider",
    *,
    repositories: RepositoriesService | None = None,
    session_ttl_seconds: int = 3600,
) -> Any:
    service = repositories or RepositoriesService(
        gateway=FakeInstallationGateway(),
        cache=InMemoryRepositoriesCache(clock),
        clock=clock,
        cache_ttl=timedelta(seconds=60),
    )
    return create_app(
        settings=make_settings(session_ttl_seconds=session_ttl_seconds),
        identity_provider=identity,
        session_store=InMemorySessionStore(clock),
        attempt_store=InMemorySignInAttemptStore(clock),
        clock=clock,
        repositories=service,
        app_slug=APP_SLUG,
    )


@pytest.fixture
def auth_clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def gateway() -> FakeInstallationGateway:
    return FakeInstallationGateway()


@pytest.fixture
def repositories_cache(auth_clock: FixedClock) -> InMemoryRepositoriesCache:
    return InMemoryRepositoriesCache(auth_clock)


@pytest.fixture
def repositories_service(
    gateway: FakeInstallationGateway,
    repositories_cache: InMemoryRepositoriesCache,
    auth_clock: FixedClock,
) -> RepositoriesService:
    return RepositoriesService(
        gateway=gateway,
        cache=repositories_cache,
        clock=auth_clock,
        cache_ttl=timedelta(seconds=60),
    )


@pytest.fixture
def identity() -> FakeIdentityProvider:
    return FakeIdentityProvider()


@pytest.fixture
def client(
    auth_clock: FixedClock,
    identity: FakeIdentityProvider,
    repositories_service: RepositoriesService,
) -> Any:
    """TestClient over https, so the browser rules for Secure cookies apply."""
    app = build_test_app(auth_clock, identity, repositories=repositories_service)
    # Exposed so a test can assert what the store holds after re-authenticating.
    test_client_sessions = app.state.services.sessions
    with TestClient(app, base_url=TEST_ORIGIN) as test_client:
        test_client.sessions = test_client_sessions  # type: ignore[attr-defined]
        yield test_client
