"""Sign-in routes: start, callback, logout, and /me."""

import base64
import hashlib
import logging
import secrets
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from api.deps import (
    NO_STORE,
    OAUTH_COOKIE,
    SESSION_COOKIE,
    ApiError,
    Services,
    clear_oauth_cookie,
    clear_session_cookie,
    current_session,
    services,
    set_oauth_cookie,
    set_session_cookie,
)
from domain.auth import (
    Session,
    SignInAttempt,
    SignInResult,
    hash_session_id,
    sanitize_return_to,
)
from domain.errors import IdentityProviderError
from domain.tenancy import Organization, User

SUPPORTED_PROVIDERS = frozenset({"github"})
CALLBACK_PATH = "/auth/callback"
TOKEN_BYTES = 32
VERIFIER_BYTES = 64

logger = logging.getLogger(__name__)
router = APIRouter()


def _code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _callback_redirect(result: SignInResult, return_to: str | None) -> RedirectResponse:
    params: dict[str, str] = {"result": result.value}
    if return_to:
        params["return_to"] = return_to
    # Relative Location, so the browser stays on the console's origin.
    response = RedirectResponse(f"{CALLBACK_PATH}?{urlencode(params)}", status_code=302)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = NO_STORE
    clear_oauth_cookie(response)
    return response


def _require_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise ApiError(404, "unknown_provider")


@router.get("/auth/{provider}")
async def start_sign_in(provider: str, request: Request, return_to: str | None = None) -> Response:
    _require_provider(provider)
    service = services(request)

    safe_return_to = sanitize_return_to(return_to)
    state = secrets.token_urlsafe(TOKEN_BYTES)
    code_verifier = secrets.token_urlsafe(VERIFIER_BYTES)
    attempt_id = secrets.token_urlsafe(TOKEN_BYTES)

    await service.attempts.put(
        SignInAttempt(
            attempt_id=attempt_id,
            state=state,
            code_verifier=code_verifier,
            return_to=safe_return_to,
            expires_at=service.clock.now() + service.settings.oauth_state_ttl,
        )
    )

    response = RedirectResponse(
        service.identity.authorize_url(state, _code_challenge(code_verifier)),
        status_code=302,
    )
    response.headers["Cache-Control"] = NO_STORE
    response.headers["Referrer-Policy"] = "no-referrer"
    set_oauth_cookie(response, attempt_id, service.settings)
    return response


@router.get("/auth/{provider}/callback")
async def complete_sign_in(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    _require_provider(provider)
    service = services(request)

    # 1. The attempt is single use, and binds this browser to this state.
    attempt_id = request.cookies.get(OAUTH_COOKIE)
    attempt = await service.attempts.take(attempt_id) if attempt_id else None
    if attempt is None or not attempt.matches(state or ""):
        logger.info("sign-in rejected", extra={"result": SignInResult.STATE_MISMATCH.value})
        return _callback_redirect(SignInResult.STATE_MISMATCH, None)

    # 2. The user cancelled on GitHub.
    if error:
        logger.info("sign-in rejected", extra={"result": SignInResult.ACCESS_DENIED.value})
        return _callback_redirect(SignInResult.ACCESS_DENIED, attempt.return_to)

    if not code:
        return _callback_redirect(SignInResult.SERVER_ERROR, attempt.return_to)

    # 3. Exchange and load. The token never leaves this block.
    try:
        token = await service.identity.exchange_code(code, attempt.code_verifier)
        profile = await service.identity.load_profile(token)
    except IdentityProviderError as failure:
        logger.warning(
            "sign-in failed",
            extra={"result": SignInResult.SERVER_ERROR.value, "reason": failure.reason},
        )
        return _callback_redirect(SignInResult.SERVER_ERROR, attempt.return_to)

    # 4. Replace any session this browser already holds, then issue a brand new
    #    id. Re-authenticating through /github/setup must not leave the previous
    #    session alive: one browser, one session, and signing out ends it.
    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        await service.sessions.delete(hash_session_id(previous))

    # A new id, never a value the browser already had: no session fixation.
    raw_session_id = secrets.token_urlsafe(TOKEN_BYTES)
    now = service.clock.now()
    session = Session.issue(
        id_hash=hash_session_id(raw_session_id),
        user=profile.user,
        organizations=profile.organizations,
        now=now,
        ttl=service.settings.session_ttl,
    )
    await service.sessions.create(session)

    # An audit record: who signed in and when. Never the code, state or token.
    logger.info(
        "sign-in succeeded",
        extra={
            "result": SignInResult.SUCCESS.value,
            "user_id": profile.user.id,
            "organizations": len(profile.organizations),
        },
    )
    response = _callback_redirect(SignInResult.SUCCESS, attempt.return_to)
    set_session_cookie(response, raw_session_id, service.settings)
    return response


@router.post("/auth/logout")
async def logout(request: Request) -> Response:
    """Idempotent: 204 whether or not there was a session."""
    service = services(request)
    raw_value = request.cookies.get(SESSION_COOKIE)
    if raw_value:
        await service.sessions.delete(hash_session_id(raw_value))
        logger.info("session ended")
    response = Response(status_code=204)
    response.headers["Cache-Control"] = NO_STORE
    clear_session_cookie(response)
    return response


@router.get("/me")
async def read_me(session: Annotated[Session, Depends(current_session)]) -> dict[str, Any]:
    """Returns a dict, not a Response: the cookie and Cache-Control that
    `current_session` set on the injected Response are only applied when the
    route does not build its own."""
    return {
        "user": _user_json(session.user),
        "organizations": [_organization_json(org) for org in session.organizations],
        "current_organization_id": session.current_organization_id,
    }


def _user_json(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "login": user.login,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "is_platform_admin": user.is_platform_admin,
    }


def _organization_json(organization: Organization) -> dict[str, Any]:
    return {
        "id": organization.id,
        "login": organization.login,
        "name": organization.name,
        "avatar_url": organization.avatar_url,
        "role": organization.role.value,
    }


__all__ = ["Services", "router"]
