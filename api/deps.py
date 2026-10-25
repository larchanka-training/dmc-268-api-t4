"""Request plumbing: service lookup, the session dependency, CSRF and cookies."""

from dataclasses import dataclass
from typing import cast

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from api.config import AppSettings
from domain.auth import Session, hash_session_id
from domain.ports import (
    Clock,
    IdentityProvider,
    JobEventsSource,
    JobHistory,
    ReviewCommentStore,
    SessionStore,
    SignInAttemptStore,
)
from domain.repositories import RepositoriesService

SESSION_COOKIE = "__Host-session"
OAUTH_COOKIE = "__Host-oauth"
NO_STORE = "no-store"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_REQUESTED_WITH = "fetch"
_WEBHOOK_PREFIX = "/webhooks/"


class ApiError(Exception):
    """An error rendered as {"error": code}, which is the agreed wire shape."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def api_error_response(error: ApiError) -> JSONResponse:
    return JSONResponse(
        {"error": error.code},
        status_code=error.status_code,
        headers={"Cache-Control": NO_STORE},
    )


@dataclass(frozen=True, slots=True)
class Services:
    """Everything a request may need, wired once by the app factory."""

    settings: AppSettings
    identity: IdentityProvider
    sessions: SessionStore
    attempts: SignInAttemptStore
    clock: Clock
    repositories: RepositoriesService
    app_slug: str
    job_events: JobEventsSource
    jobs: JobHistory
    findings: ReviewCommentStore


def services(request: Request) -> Services:
    return cast(Services, request.app.state.services)


def set_session_cookie(response: Response, raw_value: str, settings: AppSettings) -> None:
    """__Host- forbids a Domain attribute and requires Secure with Path=/."""
    response.set_cookie(
        SESSION_COOKIE,
        raw_value,
        max_age=settings.session_ttl_seconds,
        path="/",
        httponly=True,
        secure=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        "",
        max_age=0,
        path="/",
        httponly=True,
        secure=True,
        samesite="strict",
    )


def set_oauth_cookie(response: Response, attempt_id: str, settings: AppSettings) -> None:
    """Lax, not Strict: this one must survive GitHub's cross-site redirect back."""
    response.set_cookie(
        OAUTH_COOKIE,
        attempt_id,
        max_age=settings.oauth_state_ttl_seconds,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )


def clear_oauth_cookie(response: Response) -> None:
    response.set_cookie(
        OAUTH_COOKIE,
        "",
        max_age=0,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )


async def current_session(request: Request, response: Response) -> Session:
    """Load the session, slide its idle timeout, and re-send the cookie."""
    service = services(request)
    raw_value = request.cookies.get(SESSION_COOKIE)
    if not raw_value:
        raise ApiError(401, "no_session")

    session = await service.sessions.get(hash_session_id(raw_value))
    now = service.clock.now()
    if session is None or session.is_expired(now):
        raise ApiError(401, "no_session")

    refreshed = session.touch(now, service.settings.session_ttl)
    await service.sessions.save(refreshed)
    set_session_cookie(response, raw_value, service.settings)
    response.headers["Cache-Control"] = NO_STORE
    return refreshed


async def optional_session(request: Request) -> Session | None:
    """The session if there is a live one, else None. Does not slide the timeout:
    the only caller is /github/setup, which GitHub redirects to, not the SPA."""
    raw_value = request.cookies.get(SESSION_COOKIE)
    if not raw_value:
        return None
    service = services(request)
    session = await service.sessions.get(hash_session_id(raw_value))
    if session is None or session.is_expired(service.clock.now()):
        return None
    return session


def enforce_csrf(request: Request) -> JSONResponse | None:
    """Unsafe methods need X-Requested-With: fetch and our own Origin."""
    # Webhook deliveries are forge-to-server POSTs: they carry no cookies
    # and no Origin or X-Requested-With headers, so the checks below would
    # reject every legitimate delivery. Their authentication is the
    # delivery signature, verified in the route
    # (docs/WORKFLOW_DESIGN.md §2 Step 2).
    # Behind uvicorn --root-path /api the request path carries the prefix
    # (/api/webhooks/github), so match on the path the router sees.
    route_path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
    if route_path.startswith(_WEBHOOK_PREFIX):
        return None
    if request.method in _SAFE_METHODS:
        return None
    if request.headers.get("x-requested-with") != _REQUESTED_WITH:
        return api_error_response(ApiError(403, "csrf"))
    origin = request.headers.get("origin")
    if origin != services(request).settings.app_origin:
        return api_error_response(ApiError(403, "csrf"))
    return None
