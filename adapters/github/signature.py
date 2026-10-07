"""Verifying GitHub webhook deliveries.

Implements Step 2 of [docs/WORKFLOW_DESIGN.md §2](../../docs/WORKFLOW_DESIGN.md):
GitHub authenticates a delivery with HMAC-SHA256 over the **raw** body, sent
as `X-Hub-Signature-256: sha256=<hex>` and compared with `hmac.compare_digest`.
The signature is computed over the bytes exactly as received; re-serializing
parsed JSON changes whitespace and key order and will not match.

Neither the secret, nor the provided digest, nor any part of the body ever
appears in an exception message (AGENTS.md hard rule 2).
"""

import hashlib
import hmac

from adapters.github.config import PROVIDER_NAME
from domain.errors import WebhookSignatureError

SIGNATURE_HEADER = "X-Hub-Signature-256"
_SIGNATURE_PREFIX = "sha256="
# hashlib.sha256().hexdigest() is always 64 lowercase hex characters.
_DIGEST_LENGTH = 64


def verify_webhook_signature(raw_body: bytes, header: str | None, secret: str) -> None:
    """Verify `X-Hub-Signature-256` over the raw body; raise on any failure.

    Returns None on success. On failure the reason — and only the reason —
    escapes: no secret, no digest, no body content.
    """
    if header is None:
        raise WebhookSignatureError(PROVIDER_NAME, f"{SIGNATURE_HEADER} header is missing")
    if not header.startswith(_SIGNATURE_PREFIX):
        raise WebhookSignatureError(
            PROVIDER_NAME, f"{SIGNATURE_HEADER} must start with '{_SIGNATURE_PREFIX}'"
        )
    provided = header[len(_SIGNATURE_PREFIX) :].lower()
    if len(provided) != _DIGEST_LENGTH:
        raise WebhookSignatureError(
            PROVIDER_NAME,
            f"digest must be {_DIGEST_LENGTH} hex characters, got {len(provided)}",
        )
    try:
        bytes.fromhex(provided)
    except ValueError:
        raise WebhookSignatureError(PROVIDER_NAME, "digest is not valid hex") from None
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise WebhookSignatureError(PROVIDER_NAME, "digest does not match the raw body")
