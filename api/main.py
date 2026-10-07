"""App factory. Production wiring builds the real adapters; tests pass fakes."""

import logging
from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from adapters.clock import SystemClock
from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import GitHubAppSettings
from adapters.github.factory import build_vcs_client
from adapters.github.installation import GitHubInstallationGateway
from adapters.identity.config import GitHubIdentitySettings
from adapters.identity.github import GitHubIdentityProvider
from adapters.jobs.memory import MemoryJobStore
from adapters.memory.auth import InMemorySessionStore, InMemorySignInAttemptStore
from adapters.memory.repositories import InMemoryRepositoriesCache
from api.auth import router as auth_router
from api.config import AppSettings
from api.deps import (
    ApiError,
    CallbackQueryLogFilter,
    Services,
    api_error_response,
    enforce_csrf,
)
from api.github_setup import router as github_setup_router
from api.repositories import router as repositories_router
from api.webhooks.github import GitHubWebhookDeps
from api.webhooks.github import router as github_webhook_router
from domain.ports import (
    Clock,
    GitProvider,
    IdentityProvider,
    JobRepository,
    SessionStore,
    SignInAttemptStore,
)
from domain.repositories import RepositoriesService

Next = Callable[[Request], Awaitable[Response]]


def create_app(
    settings: AppSettings,
    identity_provider: IdentityProvider,
    session_store: SessionStore,
    attempt_store: SignInAttemptStore,
    clock: Clock,
    repositories: RepositoriesService,
    app_slug: str,
    jobs: JobRepository,
    vcs: GitProvider,
    webhook_secret: str,
) -> FastAPI:
    app = FastAPI(title="DMC-268 API", version="0.1.0")
    app.state.services = Services(
        settings=settings,
        identity=identity_provider,
        sessions=session_store,
        attempts=attempt_store,
        clock=clock,
        repositories=repositories,
        app_slug=app_slug,
    )
    app.state.webhooks = GitHubWebhookDeps(jobs=jobs, vcs=vcs, webhook_secret=webhook_secret)

    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, ApiError)
        return api_error_response(error)

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next: Next) -> Response:
        rejected = enforce_csrf(request)
        if rejected is not None:
            return rejected
        return await call_next(request)

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"message": "Welcome to DMC-268 Team 4 API"}

    @app.get("/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(repositories_router)
    app.include_router(github_setup_router)
    app.include_router(github_webhook_router)
    return app


def install_access_log_filter() -> None:
    """Keep the authorization code out of uvicorn's access log."""
    logging.getLogger("uvicorn.access").addFilter(CallbackQueryLogFilter())


def build_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Production wiring. Sessions and attempts are in memory until the database
    exists; `adapters/db/auth.py` replaces them behind the same ports."""
    install_access_log_filter()
    clock = SystemClock()
    settings = AppSettings.from_env()
    app_settings = GitHubAppSettings.from_env()
    app_auth = GitHubAppAuth(app_settings, clock, transport=transport)
    return create_app(
        settings=settings,
        identity_provider=GitHubIdentityProvider(
            GitHubIdentitySettings.from_env(), transport=transport
        ),
        session_store=InMemorySessionStore(clock),
        attempt_store=InMemorySignInAttemptStore(clock),
        clock=clock,
        repositories=RepositoriesService(
            gateway=GitHubInstallationGateway(app_settings, app_auth, transport=transport),
            cache=InMemoryRepositoriesCache(clock),
            clock=clock,
            cache_ttl=settings.repositories_cache_ttl,
        ),
        app_slug=app_settings.app_slug,
        # In-memory queue until the PostgreSQL one exists (tasks/plan.md
        # decision 1, 2026-10-07); same port, swapped here.
        jobs=MemoryJobStore(clock=clock),
        vcs=build_vcs_client(app_settings, transport=transport, clock=clock),
        webhook_secret=app_settings.webhook_secret,
    )
