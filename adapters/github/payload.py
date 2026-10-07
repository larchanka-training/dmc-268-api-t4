"""Routing on, and parsing, GitHub `pull_request` webhook payloads.

Implements Step 2 of [docs/WORKFLOW_DESIGN.md §2](../../docs/WORKFLOW_DESIGN.md):
route on the event, then filter on the action before parsing. The DTO is a
wire shape, so it lives here in the adapter rather than in `domain/`. Error
messages name the missing or malformed field only, never payload content
(AGENTS.md hard rule 6).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from adapters.github.config import PROVIDER_NAME
from domain.errors import WebhookPayloadError


class PullRequestAction(StrEnum):
    OPENED = "opened"
    SYNCHRONIZE = "synchronize"
    REOPENED = "reopened"
    READY_FOR_REVIEW = "ready_for_review"


#: The actions that trigger a review (docs/WORKFLOW_DESIGN.md §2, Step 1).
REVIEWABLE_ACTIONS: frozenset[PullRequestAction] = frozenset(PullRequestAction)


@dataclass(frozen=True, slots=True)
class PullRequestEvent:
    """The fields the review pipeline needs from a `pull_request` delivery."""

    action: PullRequestAction
    installation_id: int
    repo_id: int
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str
    title: str
    author_login: str | None
    author_external_id: int | None
    author_association: str | None
    draft: bool


def reviewable_action(payload: Mapping[str, Any]) -> PullRequestAction | None:
    """The delivery's action if it should trigger a review, else None.

    Routing check, called before `parse_pull_request_event`: unknown or
    non-reviewable actions are not errors, they simply do not review.
    """
    raw = payload.get("action")
    if not isinstance(raw, str):
        return None
    try:
        action = PullRequestAction(raw)
    except ValueError:
        return None
    return action if action in REVIEWABLE_ACTIONS else None


def parse_pull_request_event(payload: Mapping[str, Any]) -> PullRequestEvent:
    """Validate an accepted `pull_request` payload and map it to the DTO.

    Callers route with `reviewable_action` first; anything malformed raises
    `WebhookPayloadError` naming the field, never quoting the payload.
    """
    raw_action = payload.get("action")
    if not isinstance(raw_action, str):
        raise WebhookPayloadError(PROVIDER_NAME, "action missing or not a string")
    try:
        action = PullRequestAction(raw_action)
    except ValueError:
        raise WebhookPayloadError(
            PROVIDER_NAME, "action is not a reviewable pull_request action"
        ) from None

    installation = _object_of(payload, "installation")
    repository = _object_of(payload, "repository")
    pull_request = _object_of(payload, "pull_request")

    user = pull_request.get("user")
    if user is None:
        author_login = None
        author_external_id = None
    elif isinstance(user, Mapping):
        author_login = _string_of(user, "login", "pull_request.user.login")
        author_external_id = _int_of(user, "id", "pull_request.user.id")
    else:
        raise WebhookPayloadError(PROVIDER_NAME, "pull_request.user is not an object or null")

    association = pull_request.get("author_association")
    author_association = association if isinstance(association, str) else None

    return PullRequestEvent(
        action=action,
        installation_id=_int_of(installation, "id", "installation.id"),
        repo_id=_int_of(repository, "id", "repository.id"),
        repo_full_name=_string_of(repository, "full_name", "repository.full_name"),
        pr_number=_int_of(pull_request, "number", "pull_request.number"),
        head_sha=_sha_of(_object_of(pull_request, "head"), "sha", "pull_request.head.sha"),
        base_sha=_sha_of(_object_of(pull_request, "base"), "sha", "pull_request.base.sha"),
        title=_string_of(pull_request, "title", "pull_request.title"),
        author_login=author_login,
        author_external_id=author_external_id,
        author_association=author_association,
        draft=_bool_of(pull_request, "draft", "pull_request.draft"),
    )


def _object_of(section: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = section.get(key)
    if not isinstance(value, Mapping):
        raise WebhookPayloadError(PROVIDER_NAME, f"{key} missing or not an object")
    return value


def _int_of(section: Mapping[str, Any], key: str, path: str) -> int:
    value = section.get(key)
    # bool is an int subclass; GitHub never sends one for an id.
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebhookPayloadError(PROVIDER_NAME, f"{path} missing or not an int")
    return value


def _string_of(section: Mapping[str, Any], key: str, path: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value:
        raise WebhookPayloadError(PROVIDER_NAME, f"{path} missing or not a string")
    return value


def _bool_of(section: Mapping[str, Any], key: str, path: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise WebhookPayloadError(PROVIDER_NAME, f"{path} missing or not a bool")
    return value


def _sha_of(section: Mapping[str, Any], key: str, path: str) -> str:
    value = _string_of(section, key, path)
    if not _is_sha(value):
        raise WebhookPayloadError(PROVIDER_NAME, f"{path} is not a 40-character hex sha")
    return value


_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _is_sha(value: str) -> bool:
    return _SHA_RE.fullmatch(value) is not None
