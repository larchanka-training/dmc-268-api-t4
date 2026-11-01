from collections.abc import Mapping, Sequence


class LLMGatewayError(Exception):
    """Base class for every error the LLM gateway raises to its callers."""


class LLMConfigurationError(LLMGatewayError):
    """The gateway cannot be built from the given settings."""


class ProviderUnavailableError(LLMGatewayError):
    """``detail`` is the start of the provider's response body: useful to a
    caller reading the exception, but it may echo the request, so a log line
    carries ``reason`` only (docs/observability.md § What is never logged)."""

    def __init__(self, provider: str, reason: str, *, detail: str | None = None) -> None:
        message = f"LLM provider '{provider}' unavailable: {reason}"
        super().__init__(f"{message}: {detail}" if detail else message)
        self.provider = provider
        self.reason = reason
        self.detail = detail


class LLMOutputError(LLMGatewayError):
    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"LLM provider '{provider}' returned unusable output: {reason}")
        self.provider = provider
        self.reason = reason


class LLMInputTooLargeError(LLMGatewayError):
    """The review input exceeds the token budget; no provider is called."""

    def __init__(self, estimated_tokens: int, limit: int) -> None:
        super().__init__(f"review input is too large: ~{estimated_tokens} tokens, limit {limit}")
        self.estimated_tokens = estimated_tokens
        self.limit = limit


class AllProvidersFailedError(LLMGatewayError):
    def __init__(self, causes: Sequence[LLMGatewayError]) -> None:
        self.causes: tuple[LLMGatewayError, ...] = tuple(causes)
        details = "; ".join(str(cause) for cause in self.causes)
        super().__init__(f"all LLM providers failed ({len(self.causes)}): {details}")


class AuthError(Exception):
    """Base class for every error the sign-in flow raises to its callers."""


class AuthConfigurationError(AuthError):
    """The sign-in flow cannot be built from the given settings."""


class IdentityProviderError(AuthError):
    """The identity provider could not be reached, or refused the request.

    The message carries the provider and a reason only: never a code, a token,
    a request body or a key.
    """

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"identity provider '{provider}' failed: {reason}")
        self.provider = provider
        self.reason = reason


class ForgeError(Exception):
    """Base class for errors the forge adapters raise to their callers."""


class ForgeConfigurationError(ForgeError):
    """A forge adapter cannot be built from the given settings."""


class InstallationGoneError(ForgeError):
    """The installation no longer grants access: uninstalled or suspended.

    Recoverable at the product level — the caller drops its cache and reports an
    empty list, and the next sign-in refreshes the organisations.
    """

    def __init__(self, installation_id: int, reason: str) -> None:
        super().__init__(f"installation {installation_id} is gone: {reason}")
        self.installation_id = installation_id
        self.reason = reason


class ForgeUnavailableError(ForgeError):
    """The forge could not be reached, or failed for a reason we cannot act on."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"forge '{provider}' unavailable: {reason}")
        self.provider = provider
        self.reason = reason


class PrNotFoundError(ForgeError):
    """The pull request no longer exists on the forge (HTTP 404).

    Permanent, not retryable (docs/PIPELINE_SPEC.md §4.2): retrying cannot
    bring a deleted pull request back. Mirrors ForgeUnavailableError.
    """

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"forge '{provider}': {reason}")
        self.provider = provider
        self.reason = reason


class ForgeRateLimitedError(ForgeUnavailableError):
    """The forge throttled us (429, or a 403 secondary rate limit). Retryable.

    `retry_after_seconds` is the wait the forge asked for, when it gave one.
    """

    def __init__(self, provider: str, retry_after_seconds: int | None) -> None:
        super().__init__(provider, "rate limited")
        self.retry_after_seconds = retry_after_seconds


class ForgePermissionError(ForgeError):
    """The installation lacks a permission the call needs. Permanent until an admin acts.

    For publishing that is `Pull requests: write` and `Checks: write` on the GitHub App;
    after the App's permissions are raised, every installation must accept them.
    """

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"forge '{provider}' refused for lack of permission: {reason}")
        self.provider = provider
        self.reason = reason


class WebhookSignatureError(ForgeError):
    """A webhook delivery did not authenticate.

    The message carries the provider and a reason only: never the secret, the
    provided digest, or any part of the body (AGENTS.md hard rule 2).
    """

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"{provider} webhook signature verification failed: {reason}")
        self.provider = provider
        self.reason = reason


class WebhookPayloadError(ForgeError):
    """A webhook payload did not match the provider's documented shape.

    The message names the missing or malformed field only: never any payload
    content (AGENTS.md hard rule 6).
    """

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"{provider} webhook payload is malformed: {reason}")
        self.provider = provider
        self.reason = reason


class DBConfigurationError(Exception):
    """The database adapter cannot be built from the given settings."""


class WorkerConfigurationError(Exception):
    """The worker process cannot be built from its settings."""


class ObservabilityConfigurationError(Exception):
    """Logging or error reporting cannot be set up from the given settings."""


class DiffFormatError(Exception):
    """The diff text is not a well-formed unified git diff.

    The message carries a reason and the offending line number only — never
    diff content (AGENTS.md hard rule 3).
    """

    def __init__(self, reason: str, line_number: int) -> None:
        super().__init__(f"malformed diff at line {line_number}: {reason}")
        self.reason = reason
        self.line_number = line_number


#: Every error_kind ``classify`` returns, and whether the job is retried for it: the
#: closed error vocabulary of docs/PIPELINE_SPEC.md §4.2/§5. The job metrics start a
#: series at 0 for each (docs/observability.md § Metrics).
ERROR_KINDS: Mapping[str, bool] = {
    "forge_unavailable": True,
    "installation_removed": False,
    "pr_not_found": False,
    "diff_parse_failed": False,
    "provider_unavailable": True,
    "llm_output_invalid": False,
    "skip_diff_too_large": False,
    "internal_error": False,
}


def classify(error: BaseException) -> tuple[str, bool]:
    """Map an exception to (error_kind, retryable). Anything not named here is
    ``internal_error``: a bug, not an outage, so it is not retried."""
    kind = error_kind(error)
    return kind, ERROR_KINDS[kind]


def error_kind(error: BaseException) -> str:
    if isinstance(error, ForgeUnavailableError):
        return "forge_unavailable"
    if isinstance(error, InstallationGoneError):
        return "installation_removed"
    if isinstance(error, PrNotFoundError):
        return "pr_not_found"
    if isinstance(error, DiffFormatError):
        return "diff_parse_failed"
    if isinstance(error, (AllProvidersFailedError, ProviderUnavailableError)):
        return "provider_unavailable"
    if isinstance(error, LLMOutputError):
        return "llm_output_invalid"
    if isinstance(error, LLMInputTooLargeError):
        return "skip_diff_too_large"
    return "internal_error"


class ContextUnavailableError(Exception):
    """The code behind a finding cannot be rebuilt from the forge: the installation or a
    reviewed commit is gone, or the finding's line is not in the diff any more.

    The reason is a short fixed phrase for logs, never repository content.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"context unavailable: {reason}")
        self.reason = reason
