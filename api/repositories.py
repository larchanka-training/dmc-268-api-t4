"""The console's repository endpoints."""

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from api.deps import ApiError, current_session, services
from domain.auth import Session
from domain.errors import ForgeUnavailableError
from domain.repositories import (
    Repository,
    RepositoryPage,
    connect_url,
    installation_settings_url,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/repositories")
async def list_repositories(
    request: Request,
    session: Annotated[Session, Depends(current_session)],
    cursor: str | None = None,
) -> dict[str, Any]:
    service = services(request)
    try:
        page = await service.repositories.list_page(session, cursor)
    except ValueError:
        # A malformed or negative cursor. The value is echoed nowhere.
        raise ApiError(400, "bad_cursor") from None
    except ForgeUnavailableError as failure:
        logger.warning("repository listing failed", extra={"reason": failure.reason})
        raise ApiError(502, "forge_unavailable") from None
    return _page_json(page)


@router.get("/repositories/connect-url")
async def read_connect_url(
    request: Request,
    session: Annotated[Session, Depends(current_session)],
) -> dict[str, str]:
    """Members get the URL too: GitHub refuses non-owners on its side, and the
    console already disables the button for them. Reading it changes nothing, so
    it stays a GET and needs no CSRF header."""
    service = services(request)
    return {"url": connect_url(session.current_organization, service.app_slug)}


@router.get("/repositories/{repository_id}/disconnect-url")
async def read_disconnect_url(
    repository_id: str,
    request: Request,
    session: Annotated[Session, Depends(current_session)],
) -> dict[str, str]:
    """Where to remove this repository from the installation.

    Per repository rather than one shared endpoint so that a repository which is
    already gone answers 404 instead of sending the user to GitHub for nothing.
    The URL itself is the installation's settings page: GitHub has no
    per-repository removal URL.
    """
    organization = session.current_organization
    if organization is None:
        # Nothing is connected, so no repository can be.
        raise ApiError(404, "not_found")
    service = services(request)
    if await service.repositories.find(session, repository_id) is None:
        raise ApiError(404, "not_found")
    return {"url": installation_settings_url(organization)}


def _page_json(page: RepositoryPage) -> dict[str, Any]:
    return {
        "items": [_repository_json(repository) for repository in page.items],
        "next_cursor": page.next_cursor,
        "total_count": page.total_count,
    }


def _repository_json(repository: Repository) -> dict[str, Any]:
    return {
        "id": repository.id,
        "owner": repository.owner,
        "name": repository.name,
        "private": repository.private,
        "default_branch": repository.default_branch,
        "html_url": repository.html_url,
        "connected_at": _iso(repository.connected_at),
        "last_run_at": _iso(repository.last_run_at),
    }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()
