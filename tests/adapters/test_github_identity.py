"""The GitHub adapter against recorded fixtures. No test opens a connection."""

import httpx
import pytest

from adapters.identity.config import GitHubIdentitySettings
from adapters.identity.github import GitHubIdentityProvider
from domain.errors import IdentityProviderError
from domain.ports import Token
from domain.tenancy import AccountType, OrganizationRole
from tests.conftest import FAKE_TOKEN, RecordingTransport, github_fixture

SETTINGS = GitHubIdentitySettings(
    client_id="Iv23li-test",
    client_secret="gh-secret-test-value",
    callback_url="http://localhost:5173/api/auth/github/callback",
    api_base_url="https://api.github.test",
    token_url="https://github.test/login/oauth/access_token",
    authorize_url="https://github.test/login/oauth/authorize",
)

MEMBERSHIP_BY_LOGIN = {"acme": "membership_admin", "pending-org": "membership_pending"}


def github_handler(user: str = "user", *, membership_status: int = 200) -> RecordingTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user":
            return httpx.Response(200, json=github_fixture(user))
        if path == "/user/installations":
            return httpx.Response(200, json=github_fixture("installations"))
        if path.startswith("/user/memberships/orgs/"):
            if membership_status != 200:
                return httpx.Response(membership_status, json={"message": "Not Found"})
            login = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=github_fixture(MEMBERSHIP_BY_LOGIN[login]))
        raise AssertionError(f"unexpected path {path}")

    return RecordingTransport(handler)


def provider(transport: httpx.AsyncBaseTransport) -> GitHubIdentityProvider:
    return GitHubIdentityProvider(SETTINGS, transport=transport)


# --- authorize_url ------------------------------------------------------


def test_authorize_url_carries_pkce_and_the_registered_redirect() -> None:
    url = httpx.URL(provider(github_handler()).authorize_url("the-state", "the-challenge"))

    assert str(url).startswith(SETTINGS.authorize_url)
    assert dict(url.params) == {
        "client_id": SETTINGS.client_id,
        "redirect_uri": SETTINGS.callback_url,
        "state": "the-state",
        "code_challenge": "the-challenge",
        "code_challenge_method": "S256",
    }


def test_authorize_url_never_carries_the_client_secret() -> None:
    url = provider(github_handler()).authorize_url("s", "c")

    assert SETTINGS.client_secret not in url


# --- exchange_code ------------------------------------------------------


async def test_exchange_code_posts_the_documented_body() -> None:
    transport = RecordingTransport(lambda _: httpx.Response(200, json=github_fixture("token")))

    token = await provider(transport).exchange_code("the-code", "the-verifier")

    assert token.access_token == FAKE_TOKEN
    assert token.expires_in == 28800
    assert token.refresh_token is not None
    sent = transport.requests[0]
    assert sent.method == "POST"
    assert sent.headers["accept"] == "application/json"
    body = dict(httpx.QueryParams(sent.content.decode()))
    assert body == {
        "client_id": SETTINGS.client_id,
        "client_secret": SETTINGS.client_secret,
        "code": "the-code",
        "redirect_uri": SETTINGS.callback_url,
        "code_verifier": "the-verifier",
    }


async def test_an_error_field_raises_without_echoing_the_code() -> None:
    transport = RecordingTransport(
        lambda _: httpx.Response(
            200,
            json={"error": "bad_verification_code", "error_description": "code the-code is bad"},
        )
    )

    with pytest.raises(IdentityProviderError) as caught:
        await provider(transport).exchange_code("the-code", "v")

    assert "bad_verification_code" in str(caught.value)
    assert "the-code" not in str(caught.value)


async def test_a_non_200_status_raises() -> None:
    transport = RecordingTransport(lambda _: httpx.Response(502, text="bad gateway"))

    with pytest.raises(IdentityProviderError, match="HTTP 502"):
        await provider(transport).exchange_code("c", "v")


async def test_a_missing_access_token_raises() -> None:
    transport = RecordingTransport(lambda _: httpx.Response(200, json={"token_type": "bearer"}))

    with pytest.raises(IdentityProviderError, match="no access_token"):
        await provider(transport).exchange_code("c", "v")


async def test_a_timeout_raises_an_identity_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    with pytest.raises(IdentityProviderError, match="timed out"):
        await provider(RecordingTransport(handler)).exchange_code("c", "v")


# --- load_profile -------------------------------------------------------


async def test_load_profile_maps_user_and_organizations() -> None:
    profile = await provider(github_handler()).load_profile(Token(access_token=FAKE_TOKEN))

    assert profile.user.id == "1"
    assert profile.user.login == "octocat"
    assert profile.user.name == "Octo Cat"

    by_login = {org.login: org for org in profile.organizations}
    # acme is an active admin -> owner; the app on the user's own account -> owner.
    assert set(by_login) == {"acme", "octocat"}
    assert by_login["acme"].role is OrganizationRole.OWNER
    assert by_login["acme"].name == "Acme Inc"
    assert by_login["octocat"].role is OrganizationRole.OWNER
    # name is null in the fixture, so it falls back to the login.
    assert by_login["octocat"].name == "octocat"


async def test_load_profile_keeps_the_installation_id_and_account_type() -> None:
    """Spec 002 Step 0: the repositories feature needs both, so they must not be
    dropped during mapping."""
    profile = await provider(github_handler()).load_profile(Token(access_token=FAKE_TOKEN))

    by_login = {org.login: org for org in profile.organizations}
    # installations.json: acme is installation 10, the user's own account is 12.
    assert by_login["acme"].installation_id == 10
    assert by_login["acme"].account_type is AccountType.ORGANIZATION
    assert by_login["octocat"].installation_id == 12
    assert by_login["octocat"].account_type is AccountType.USER


async def test_an_installation_without_a_numeric_id_is_skipped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json=github_fixture("user"))
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [{"account": {"id": 1, "login": "octocat", "type": "User"}}]
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    profile = await provider(RecordingTransport(handler)).load_profile(
        Token(access_token=FAKE_TOKEN)
    )

    assert profile.organizations == ()


async def test_an_unknown_account_type_is_skipped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json=github_fixture("user"))
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {"id": 9, "account": {"id": 5, "login": "bot", "type": "Mannequin"}}
                    ]
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    profile = await provider(RecordingTransport(handler)).load_profile(
        Token(access_token=FAKE_TOKEN)
    )

    assert profile.organizations == ()


async def test_a_null_user_name_falls_back_to_the_login() -> None:
    profile = await provider(github_handler("user_null_name")).load_profile(
        Token(access_token=FAKE_TOKEN)
    )

    assert profile.user.name == "octocat"


async def test_a_pending_membership_is_left_out() -> None:
    profile = await provider(github_handler()).load_profile(Token(access_token=FAKE_TOKEN))

    assert "pending-org" not in {org.login for org in profile.organizations}


async def test_another_users_account_is_left_out() -> None:
    profile = await provider(github_handler()).load_profile(Token(access_token=FAKE_TOKEN))

    assert "someone-else" not in {org.login for org in profile.organizations}


async def test_an_unreadable_membership_drops_only_that_organization() -> None:
    profile = await provider(github_handler(membership_status=403)).load_profile(
        Token(access_token=FAKE_TOKEN)
    )

    assert {org.login for org in profile.organizations} == {"octocat"}


async def test_the_token_is_sent_as_a_bearer_header() -> None:
    transport = github_handler()

    await provider(transport).load_profile(Token(access_token=FAKE_TOKEN))

    assert transport.requests[0].headers["authorization"] == f"Bearer {FAKE_TOKEN}"


async def test_invalid_json_raises() -> None:
    transport = RecordingTransport(lambda _: httpx.Response(200, text="not json"))

    with pytest.raises(IdentityProviderError, match="invalid JSON"):
        await provider(transport).load_profile(Token(access_token=FAKE_TOKEN))


def test_the_token_is_not_in_its_repr() -> None:
    assert FAKE_TOKEN not in repr(Token(access_token=FAKE_TOKEN, refresh_token="ghr-secret"))
    assert "ghr-secret" not in repr(Token(access_token=FAKE_TOKEN, refresh_token="ghr-secret"))
