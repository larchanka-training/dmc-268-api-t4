"""The console's repository endpoints."""

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from api.deps import ApiError, current_session, services
from domain.auth import Session
from domain.errors import ForgeUnavailableError, InstallationGoneError
from domain.repositories import (
    Repository,
    RepositoryPage,
    connect_url,
    installation_settings_url,
)
from domain.repository_rules import RULES_PATH, RulesPreview

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


@router.get("/repositories/{repository_id}/rules")
async def read_rules(
    repository_id: str,
    request: Request,
    session: Annotated[Session, Depends(current_session)],
    ref: str | None = None,
) -> dict[str, Any]:
    """Whether `ref` (the default branch when omitted) carries `.review/rules.md`, and
    the rules the review prompt would receive from it (docs/RULES_FORMAT_SPEC.md); the
    worker does not load the file yet (issue #37).

    Read live from the forge on every call: the file changes with every push and the
    console asks for it only when the repository screen opens.
    """
    service = services(request).repository_rules
    if service is None:
        raise ApiError(502, "forge_unavailable")
    try:
        preview = await service.preview(session, repository_id, ref)
    except ValueError:
        # A malformed ref. The value is echoed nowhere.
        raise ApiError(400, "bad_ref") from None
    except InstallationGoneError:
        raise ApiError(404, "not_found") from None
    except ForgeUnavailableError as failure:
        logger.warning("rules fetch failed", extra={"reason": failure.reason})
        raise ApiError(502, "forge_unavailable") from None
    if preview is None:
        raise ApiError(404, "not_found")
    return _rules_json(preview)


def _rules_json(preview: RulesPreview) -> dict[str, Any]:
    return {
        "path": RULES_PATH,
        "ref": preview.ref,
        "status": preview.status.value,
        "file_url": preview.file_url,
        "rules": [
            {"id": rule.id, "section": rule.section, "text": rule.text}
            for rule in preview.parsed.rules
        ],
        "dropped_suspicious": preview.parsed.dropped_suspicious,
        "dropped_over_limit": preview.parsed.dropped_over_limit,
    }


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
