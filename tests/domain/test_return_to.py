"""sanitize_return_to must refuse exactly what the frontend's sanitizeReturnTo refuses."""

import pytest

from domain.auth import sanitize_return_to

ACCEPTED = [
    "/",
    "/runs",
    "/runs/42",
    "/runs?page=2",
    "/runs#top",
    "/repositories?q=a%2Fb",
    "/authentic",
    "/logins",
]

REFUSED = [
    None,
    "",
    "runs",
    "https://evil.example/runs",
    "//evil.example",
    "//evil.example/runs",
    "/\\evil.example",
    "/login",
    "/login/",
    "/login?returnTo=/runs",
    "/auth",
    "/auth/",
    "/auth/github",
    "/auth/callback?result=success",
    "/runs\nSet-Cookie: a=b",
    "/runs\r\n",
    "/runs\x00",
    "/runs\x7f",
    "\t/runs",
]


@pytest.mark.parametrize("value", ACCEPTED)
def test_same_origin_paths_are_kept(value: str) -> None:
    assert sanitize_return_to(value) == value


@pytest.mark.parametrize("value", REFUSED)
def test_unsafe_values_become_none(value: str | None) -> None:
    assert sanitize_return_to(value) is None
