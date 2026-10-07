"""Routing on, and parsing, GitHub `pull_request` webhook payloads."""

from typing import Any

import pytest

from adapters.github.payload import (
    REVIEWABLE_ACTIONS,
    PullRequestAction,
    PullRequestEvent,
    parse_pull_request_event,
    reviewable_action,
)
from domain.errors import WebhookPayloadError


def payload(action: str = "opened") -> dict[str, Any]:
    """A realistic `pull_request.opened` delivery, minimally trimmed."""
    return {
        "action": action,
        "installation": {"id": 42},
        "repository": {"id": 1_234_567, "full_name": "octo-org/hello-world"},
        "pull_request": {
            "number": 4711,
            "title": "Add a webhook parser",
            "draft": False,
            "author_association": "CONTRIBUTOR",
            "user": {"login": "octocat", "id": 583_231},
            "head": {"sha": "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3"},
            "base": {"sha": "109f4b3c50d7b0df729d299bc6f8e9ef9066971f"},
        },
    }


# --- routing: which actions are reviewable --------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("opened", PullRequestAction.OPENED),
        ("synchronize", PullRequestAction.SYNCHRONIZE),
        ("reopened", PullRequestAction.REOPENED),
        ("ready_for_review", PullRequestAction.READY_FOR_REVIEW),
    ],
)
def test_every_reviewable_action_maps(raw: str, expected: PullRequestAction) -> None:
    assert reviewable_action(payload(raw)) == expected
    assert expected in REVIEWABLE_ACTIONS


@pytest.mark.parametrize("raw", ["edited", "closed", "labeled", "bubblewrap"])
def test_non_reviewable_actions_route_to_none(raw: str) -> None:
    assert reviewable_action(payload(raw)) is None


def test_a_missing_or_non_string_action_routes_to_none() -> None:
    assert reviewable_action({}) is None
    assert reviewable_action({"action": 7}) is None


# --- parsing: the happy path ----------------------------------------------


def test_parse_extracts_every_field() -> None:
    event = parse_pull_request_event(payload())

    assert event == PullRequestEvent(
        action=PullRequestAction.OPENED,
        installation_id=42,
        repo_id=1_234_567,
        repo_full_name="octo-org/hello-world",
        pr_number=4711,
        head_sha="a94a8fe5ccb19ba61c4c0873d391e987982fbbd3",
        base_sha="109f4b3c50d7b0df729d299bc6f8e9ef9066971f",
        title="Add a webhook parser",
        author_login="octocat",
        author_external_id=583_231,
        author_association="CONTRIBUTOR",
        draft=False,
    )


def test_parse_accepts_each_reviewable_action() -> None:
    for raw in ("synchronize", "reopened", "ready_for_review"):
        event = parse_pull_request_event(payload(raw))
        assert event.action is PullRequestAction(raw)
        assert event.pr_number == 4711


def test_parse_keeps_the_draft_flag() -> None:
    body = payload()
    body["pull_request"]["draft"] = True

    assert parse_pull_request_event(body).draft is True


def test_parse_survives_a_deleted_user() -> None:
    """GitHub sends `user: null` for accounts that have since been deleted."""
    body = payload()
    body["pull_request"]["user"] = None

    event = parse_pull_request_event(body)
    assert event.author_login is None
    assert event.author_external_id is None
    assert event.author_association == "CONTRIBUTOR"


# --- parsing: malformed payloads name the field ----------------------------


_MISSING = object()


def malformed(**replacements: Any) -> dict[str, Any]:
    body = payload()
    for path, value in replacements.items():
        section: Any = body
        keys = path.split("__")
        for key in keys[:-1]:
            section = section[key]
        if value is _MISSING:
            del section[keys[-1]]
        else:
            section[keys[-1]] = value
    return body


def test_a_missing_installation_names_the_field() -> None:
    body = payload()
    del body["installation"]

    with pytest.raises(WebhookPayloadError, match=r"installation"):
        parse_pull_request_event(body)


def test_a_non_object_installation_names_the_field() -> None:
    with pytest.raises(WebhookPayloadError, match=r"installation"):
        parse_pull_request_event(malformed(installation=42))


def test_a_missing_pr_number_names_the_field() -> None:
    with pytest.raises(WebhookPayloadError, match=r"pull_request\.number"):
        parse_pull_request_event(malformed(pull_request__number=_MISSING))


def test_a_non_int_repo_id_names_the_field() -> None:
    with pytest.raises(WebhookPayloadError, match=r"repository\.id"):
        parse_pull_request_event(malformed(repository__id="1234567"))


def test_a_bool_repo_id_is_not_an_int() -> None:
    with pytest.raises(WebhookPayloadError, match=r"repository\.id"):
        parse_pull_request_event(malformed(repository__id=True))


def test_a_short_sha_names_the_field() -> None:
    with pytest.raises(WebhookPayloadError, match=r"pull_request\.head\.sha"):
        parse_pull_request_event(malformed(pull_request__head__sha="a94a8fe5"))


def test_a_non_hex_sha_names_the_field() -> None:
    with pytest.raises(WebhookPayloadError, match=r"pull_request\.base\.sha"):
        parse_pull_request_event(malformed(pull_request__base__sha="z" * 40))


def test_an_uppercase_sha_names_the_field() -> None:
    with pytest.raises(WebhookPayloadError, match=r"pull_request\.head\.sha"):
        parse_pull_request_event(
            malformed(pull_request__head__sha="A94A8FE5CCB19BA61C4C0873D391E987982FBBD3")
        )


def test_a_missing_draft_names_the_field() -> None:
    with pytest.raises(WebhookPayloadError, match=r"pull_request\.draft"):
        parse_pull_request_event(malformed(pull_request__draft=_MISSING))


def test_a_missing_action_names_the_field() -> None:
    with pytest.raises(WebhookPayloadError, match=r"\baction\b"):
        parse_pull_request_event(malformed(action=_MISSING))


def test_a_non_reviewable_action_cannot_be_parsed() -> None:
    with pytest.raises(WebhookPayloadError, match=r"\baction\b"):
        parse_pull_request_event(payload("edited"))


# --- malformed messages carry field names only ------------------------------


def test_error_messages_never_carry_payload_content() -> None:
    cases = [
        malformed(pull_request__number=_MISSING),
        malformed(repository__id="1234567"),
        malformed(pull_request__head__sha="not-a-sha"),
        malformed(pull_request__title=12345),
    ]
    messages: list[str] = []
    for case in cases:
        with pytest.raises(WebhookPayloadError) as caught:
            parse_pull_request_event(case)
        messages.append(str(caught.value))

    haystack = "\n".join(messages)
    assert "Add a webhook parser" not in haystack
    assert "octo-org/hello-world" not in haystack
    assert "octocat" not in haystack
    assert "not-a-sha" not in haystack
    assert "1234567" not in haystack
