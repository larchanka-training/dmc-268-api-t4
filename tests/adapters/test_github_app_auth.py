"""The App JWT and installation tokens, against a committed test-only key."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from adapters.github.app_auth import GitHubAppAuth, InstallationToken
from adapters.github.config import GitHubAppSettings
from domain.errors import ForgeUnavailableError, InstallationGoneError
from tests.conftest import FixedClock, RecordingTransport, github_fixture

KEY_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "github" / "app_key.pem"
APP_ID = "123456"
TOKEN_VALUE = "ghs_test_aaaaaaaaaaaaaaaaaaaaaaaa"


def settings() -> GitHubAppSettings:
    return GitHubAppSettings(
        app_id=APP_ID,
        private_key=KEY_PATH.read_text(encoding="utf-8"),
        app_slug="review-agent",
        api_base_url="https://api.github.test",
    )


def public_key() -> object:
    private = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    return private.public_key()


def auth(clock: FixedClock, transport: httpx.AsyncBaseTransport | None = None) -> GitHubAppAuth:
    return GitHubAppAuth(settings(), clock, transport=transport)


def decode(token: str) -> dict[str, object]:
    """Verify the signature, but not expiry: the JWT is minted against an injected
    clock set in the past, so wall-clock validation would always fail."""
    claims: dict[str, object] = jwt.decode(
        token,
        public_key(),  # type: ignore[arg-type]
        algorithms=["RS256"],
        options={"verify_exp": False},
    )
    return claims


# --- the App JWT --------------------------------------------------------


def test_the_jwt_is_rs256_and_verifies_against_the_key(auth_clock: FixedClock) -> None:
    token = auth(auth_clock).app_jwt()

    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = decode(token)
    assert claims["iss"] == APP_ID


def test_iat_is_backdated_and_exp_is_nine_minutes_out(auth_clock: FixedClock) -> None:
    """GitHub rejects a JWT issued in its future, and allows at most ten minutes."""
    token = auth(auth_clock).app_jwt()

    claims = decode(token)
    now = int(auth_clock.now().timestamp())
    assert claims["iat"] == now - 60
    assert claims["exp"] == now + 9 * 60
    # GitHub's cap is on exp relative to now, not on the exp-iat span (which is
    # exactly 600s here because iat is backdated).
    assert claims["exp"] <= now + 10 * 60


def test_the_jwt_moves_with_the_clock(auth_clock: FixedClock) -> None:
    first = auth(auth_clock).app_jwt()
    auth_clock.advance(120)
    second = auth(auth_clock).app_jwt()

    assert first != second


# --- installation tokens ------------------------------------------------


def token_transport(status: int = 200) -> RecordingTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"message": "nope"})
        return httpx.Response(200, json=github_fixture("installation_token"))

    return RecordingTransport(handler)


async def test_a_token_is_minted_with_the_jwt_as_bearer(auth_clock: FixedClock) -> None:
    transport = token_transport()

    value = await auth(auth_clock, transport).installation_token(42)

    assert value == TOKEN_VALUE
    sent = transport.requests[0]
    assert sent.method == "POST"
    assert str(sent.url).endswith("/app/installations/42/access_tokens")
    assert sent.headers["authorization"].startswith("Bearer ey")


async def test_the_token_is_reused_until_its_renew_margin(auth_clock: FixedClock) -> None:
    transport = token_transport()
    subject = auth(auth_clock, transport)

    await subject.installation_token(42)
    # The fixture expires at 13:00; the clock starts at 12:00 with a 5 min margin.
    auth_clock.advance(54 * 60)
    await subject.installation_token(42)

    assert len(transport.requests) == 1


async def test_the_token_is_reminted_inside_the_renew_margin(auth_clock: FixedClock) -> None:
    transport = token_transport()
    subject = auth(auth_clock, transport)

    await subject.installation_token(42)
    auth_clock.advance(56 * 60)
    await subject.installation_token(42)

    assert len(transport.requests) == 2


async def test_tokens_are_cached_per_installation(auth_clock: FixedClock) -> None:
    transport = token_transport()
    subject = auth(auth_clock, transport)

    await subject.installation_token(42)
    await subject.installation_token(43)

    assert len(transport.requests) == 2


async def test_forget_forces_a_remint(auth_clock: FixedClock) -> None:
    transport = token_transport()
    subject = auth(auth_clock, transport)

    await subject.installation_token(42)
    subject.forget(42)
    await subject.installation_token(42)

    assert len(transport.requests) == 2


@pytest.mark.parametrize("status", [403, 404])
async def test_uninstalled_or_suspended_is_installation_gone(
    auth_clock: FixedClock, status: int
) -> None:
    with pytest.raises(InstallationGoneError) as caught:
        await auth(auth_clock, token_transport(status)).installation_token(42)

    assert caught.value.installation_id == 42


async def test_another_failure_is_forge_unavailable(auth_clock: FixedClock) -> None:
    with pytest.raises(ForgeUnavailableError, match="HTTP 503"):
        await auth(auth_clock, token_transport(503)).installation_token(42)


async def test_a_timeout_is_forge_unavailable(auth_clock: FixedClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(ForgeUnavailableError, match="timed out"):
        await auth(auth_clock, RecordingTransport(handler)).installation_token(42)


async def test_a_response_without_a_token_is_forge_unavailable(auth_clock: FixedClock) -> None:
    transport = RecordingTransport(
        lambda _: httpx.Response(200, json={"expires_at": "2026-01-01T13:00:00Z"})
    )

    with pytest.raises(ForgeUnavailableError, match="no token"):
        await auth(auth_clock, transport).installation_token(42)


async def test_an_unparseable_expiry_is_forge_unavailable(auth_clock: FixedClock) -> None:
    transport = RecordingTransport(
        lambda _: httpx.Response(200, json={"token": "x", "expires_at": "soon"})
    )

    with pytest.raises(ForgeUnavailableError, match="expires_at"):
        await auth(auth_clock, transport).installation_token(42)


# --- secrets stay out of sight -----------------------------------------


def test_neither_the_key_nor_a_token_appears_in_a_repr() -> None:
    assert "PRIVATE KEY" not in repr(settings())
    expiry = datetime(2026, 1, 1, 13, tzinfo=UTC)
    assert TOKEN_VALUE not in repr(InstallationToken(value=TOKEN_VALUE, expires_at=expiry))


async def test_no_log_record_carries_the_jwt_or_the_token(
    auth_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    subject = auth(auth_clock, token_transport())
    with caplog.at_level(logging.DEBUG):
        jwt_value = subject.app_jwt()
        await subject.installation_token(42)

    ours = [record for record in caplog.records if not record.name.startswith("httpx")]
    assert ours
    haystack = "\n".join(
        record.getMessage() + " " + " ".join(str(value) for value in record.__dict__.values())
        for record in ours
    )
    assert TOKEN_VALUE not in haystack
    assert jwt_value not in haystack
