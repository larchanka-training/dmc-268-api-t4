"""App factory. Production wiring builds the real adapters; tests pass fakes."""

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from adapters.clock import SystemClock
from adapters.db.config import DBSettings
from adapters.db.job_events import PostgresJobEventsSource
from adapters.db.repository import PostgresJobStore
from adapters.db.review_comments import PostgresReviewCommentStore
from adapters.db.session import build_engine
from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import GitHubAppSettings
from adapters.github.contents import GitHubRepositoryContents
from adapters.github.factory import build_vcs_client
from adapters.github.installation import GitHubInstallationGateway
from adapters.identity.config import GitHubIdentitySettings
from adapters.identity.github import GitHubIdentityProvider
from adapters.jobs.memory import MemoryJobStore
from adapters.llm.rules import MarkdownRulesParser
from adapters.memory.auth import InMemorySessionStore, InMemorySignInAttemptStore
from adapters.memory.job_events import InMemoryJobEventsSource
from adapters.memory.repositories import InMemoryRepositoriesCache
from adapters.memory.review_comments import MemoryReviewCommentStore
from adapters.observability.config import SentrySettings
from adapters.observability.sentry import init_sentry
from api.access_log import install_access_log
from api.auth import router as auth_router
from api.config import AppSettings
from api.deps import (
    ApiError,
    Services,
    api_error_response,
    enforce_csrf,
)
from api.github_setup import router as github_setup_router
from api.jobs import router as jobs_router
from api.repositories import router as repositories_router
from api.webhooks.github import GitHubWebhookDeps
from api.webhooks.github import router as github_webhook_router
from domain.ports import (
    Clock,
    GitProvider,
    IdentityProvider,
    JobEventsSource,
    JobStore,
    ReviewCommentStore,
    SessionStore,
    SignInAttemptStore,
)
from domain.repositories import RepositoriesService
from domain.repository_rules import JobRulesService

Next = Callable[[Request], Awaitable[Response]]

logger = logging.getLogger(__name__)


def _engine_lifespan(
    engine: AsyncEngine | None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]] | None:
    """A FastAPI lifespan that disposes the app's engine on shutdown.

    None — the memory dev mode, which builds no engine — keeps FastAPI's
    default lifespan. Each process owns its engine
    (adapters/db/session.py), so disposing it here is the whole cleanup.
    """
    if engine is None:
        return None

    @asynccontextmanager
    async def dispose_engine(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    return dispose_engine


def create_app(
    settings: AppSettings,
    identity_provider: IdentityProvider,
    session_store: SessionStore,
    attempt_store: SignInAttemptStore,
    clock: Clock,
    repositories: RepositoriesService,
    app_slug: str,
    jobs: JobStore,
    vcs: GitProvider,
    webhook_secret: str,
    dispatch_inline: bool = True,
    engine: AsyncEngine | None = None,
    job_events: JobEventsSource | None = None,
    findings: ReviewCommentStore | None = None,
    job_rules: JobRulesService | None = None,
) -> FastAPI:
    app = FastAPI(title="DMC-268 API", version="0.1.0", lifespan=_engine_lifespan(engine))
    app.state.services = Services(
        settings=settings,
        identity=identity_provider,
        sessions=session_store,
        attempts=attempt_store,
        clock=clock,
        repositories=repositories,
        app_slug=app_slug,
        job_events=job_events if job_events is not None else InMemoryJobEventsSource(),
        jobs=jobs,
        findings=findings if findings is not None else MemoryReviewCommentStore(jobs),
        job_rules=job_rules,
    )
    app.state.webhooks = GitHubWebhookDeps(
        jobs=jobs,
        vcs=vcs,
        webhook_secret=webhook_secret,
        dispatch_inline=dispatch_inline,
    )

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

    # Last, so it is the outermost middleware and logs the CSRF rejections too.
    install_access_log(app)

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
    app.include_router(jobs_router)
    return app


def build_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Production wiring. Sessions and attempts are in memory until the database
    exists; `adapters/db/auth.py` replaces them behind the same ports.

    The queue follows `DATABASE_URL` (docs/WORKFLOW_DESIGN.md §8): with it
    the app enqueues into PostgreSQL and a separate worker process consumes
    (`dispatch_inline=False`, engine disposed on app shutdown); without it,
    dev mode keeps the in-memory queue and the API claims inline
    (tasks/plan-pg-queue.md)."""
    # Before the app exists, so the SDK's FastAPI integration instruments it.
    init_sentry(SentrySettings.from_env())
    clock = SystemClock()
    settings = AppSettings.from_env()
    app_settings = GitHubAppSettings.from_env()
    app_auth = GitHubAppAuth(app_settings, clock, transport=transport)
    jobs: JobStore
    dispatch_inline: bool
    engine: AsyncEngine | None = None
    if "DATABASE_URL" in os.environ:
        # PostgreSQL queue: the API only enqueues; the worker process
        # consumes (docs/WORKFLOW_DESIGN.md §8). A malformed DATABASE_URL
        # raises here, at startup — a silent fallback to memory would drop
        # real deliveries on the floor.
        engine = build_engine(DBSettings.from_env())
        jobs = PostgresJobStore(engine)
        dispatch_inline = False
    else:
        # No DATABASE_URL: dev mode keeps the in-memory queue (the
        # PostgreSQL path exists — tasks/plan-pg-queue.md) and the API
        # claims inline; no worker process is used.
        logger.info("queue.in_memory")
        jobs = MemoryJobStore(clock=clock)
        dispatch_inline = True
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
        jobs=jobs,
        vcs=build_vcs_client(app_settings, transport=transport, clock=clock),
        webhook_secret=app_settings.webhook_secret,
        dispatch_inline=dispatch_inline,
        engine=engine,
        # Without PostgreSQL no steps are recorded, so every stream answers 404.
        job_events=(
            InMemoryJobEventsSource() if engine is None else PostgresJobEventsSource(engine)
        ),
        findings=None if engine is None else PostgresReviewCommentStore(engine),
        job_rules=JobRulesService(
            jobs=jobs,
            contents=GitHubRepositoryContents(app_settings, app_auth, transport=transport),
            parser=MarkdownRulesParser(),
        ),
    )
