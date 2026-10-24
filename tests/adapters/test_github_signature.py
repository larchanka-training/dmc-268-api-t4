"""Webhook signature verification: round-trip, tampering, and secrecy."""

import hashlib
import hmac

import pytest

from adapters.github.signature import verify_webhook_signature
from domain.errors import ForgeError, WebhookSignatureError

SECRET = "whsec_test_secret_0123456789"
BODY = b'{"action":"opened","pull_request":{"number":4711}}'


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_a_valid_signature_passes() -> None:
    verify_webhook_signature(BODY, sign(BODY), SECRET)


def test_failure_is_part_of_the_forge_family() -> None:
    with pytest.raises(ForgeError):
        verify_webhook_signature(BODY, None, SECRET)


def test_a_tampered_body_is_rejected() -> None:
    with pytest.raises(WebhookSignatureError, match="does not match"):
        verify_webhook_signature(BODY + b" ", sign(BODY), SECRET)


def test_a_missing_header_is_rejected() -> None:
    with pytest.raises(WebhookSignatureError, match="missing"):
        verify_webhook_signature(BODY, None, SECRET)


def test_a_wrong_prefix_is_rejected() -> None:
    sha1 = "sha1=" + hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha1).hexdigest()
    with pytest.raises(WebhookSignatureError, match="must start with"):
        verify_webhook_signature(BODY, sha1, SECRET)


def test_a_non_hex_digest_is_rejected() -> None:
    with pytest.raises(WebhookSignatureError, match="hex"):
        verify_webhook_signature(BODY, "sha256=" + "z" * 64, SECRET)


def test_a_wrong_length_digest_is_rejected() -> None:
    with pytest.raises(WebhookSignatureError, match="64"):
        verify_webhook_signature(BODY, "sha256=" + "a" * 40, SECRET)


def test_a_signature_from_another_secret_is_rejected() -> None:
    with pytest.raises(WebhookSignatureError, match="does not match"):
        verify_webhook_signature(BODY, sign(BODY, "whsec_other_secret_9876543210"), SECRET)


def test_an_uppercase_hex_digest_still_passes() -> None:
    """The header names a hex encoding; hex digits are case-insensitive."""
    digest = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest().upper()
    verify_webhook_signature(BODY, "sha256=" + digest, SECRET)


def test_failure_messages_never_carry_secret_body_or_digest() -> None:
    digest = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    bad_headers: list[str | None] = [
        None,
        "sha1=" + "0" * 40,
        "sha256=" + "z" * 64,
        "sha256=" + digest[:63],
    ]
    messages: list[str] = []
    for header in bad_headers:
        with pytest.raises(WebhookSignatureError) as caught:
            verify_webhook_signature(BODY, header, SECRET)
        messages.append(str(caught.value))
    with pytest.raises(WebhookSignatureError) as caught:
        verify_webhook_signature(BODY, sign(BODY, "whsec_other_secret_9876543210"), SECRET)
    messages.append(str(caught.value))

    haystack = "\n".join(messages)
    assert SECRET not in haystack
    assert "whsec_other_secret_9876543210" not in haystack
    assert digest not in haystack
    assert digest[:63] not in haystack
    assert BODY.decode("utf-8") not in haystack
    assert "opened" not in haystack
    assert "4711" not in haystack
