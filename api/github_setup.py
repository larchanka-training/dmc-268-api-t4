"""The GitHub App's Setup URL.

GitHub's own warning: "Bad actors can hit this URL with a spoofed installation_id
… you should not rely on the validity of the installation_id parameter". We hold
no user token after sign-in, so rather than verify the parameter we discard it and
send the user back through sign-in. GitHub skips the consent screen for someone who
already authorised the app, and the callback's /user/installations read both
verifies the installation and refreshes the session's organisations.
"""

import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from api.deps import NO_STORE, optional_session, services
from domain.auth import Session

logger = logging.getLogger(__name__)
router = APIRouter()

# The public path, with the /api prefix the proxy strips: this is a browser redirect.
RETURN_TO = "/repositories?connected=1"
SIGN_IN_PATH = "/api/auth/github"


@router.get("/github/setup")
async def github_setup(
    request: Request,
    session: Annotated[Session | None, Depends(optional_session)],
) -> Response:
    service = services(request)
    # Best effort: the new installation is not in this session yet, which is the
    # point. The cache expires within a minute regardless.
    await service.repositories.forget(session)

    # installation_id and setup_action are deliberately never read or logged.
    logger.info("github setup redirect", extra={"had_session": session is not None})

    target = f"{SIGN_IN_PATH}?return_to={quote(RETURN_TO, safe='')}"
    response = RedirectResponse(target, status_code=302)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = NO_STORE
    return response
