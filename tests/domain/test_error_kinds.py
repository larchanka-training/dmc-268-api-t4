"""domain/errors.py: the closed error vocabulary of a review job (docs/PIPELINE_SPEC.md
§4.2/§5) and which kinds are retried."""

import pytest

from domain.errors import (
    ERROR_KINDS,
    AllProvidersFailedError,
    DiffFormatError,
    ForgeRateLimitedError,
    ForgeUnavailableError,
    InstallationGoneError,
    LLMInputTooLargeError,
    LLMOutputError,
    PrNotFoundError,
    ProviderUnavailableError,
    classify,
)


@pytest.mark.parametrize(
    ("error", "kind", "retryable"),
    [
        (ForgeUnavailableError("github", "down"), "forge_unavailable", True),
        (ForgeRateLimitedError("github", 60), "forge_unavailable", True),
        (InstallationGoneError(42, "suspended"), "installation_removed", False),
        (PrNotFoundError("github", "acme/web#7"), "pr_not_found", False),
        (DiffFormatError("no header", 1), "diff_parse_failed", False),
        (ProviderUnavailableError("eurouter", "timeout"), "provider_unavailable", True),
        (AllProvidersFailedError([]), "provider_unavailable", True),
        (LLMOutputError("eurouter", "not json"), "llm_output_invalid", False),
        (LLMInputTooLargeError(40_000, 32_000), "skip_diff_too_large", False),
        (RuntimeError("a bug"), "internal_error", False),
    ],
)
def test_each_exception_maps_to_its_kind(error: Exception, kind: str, retryable: bool) -> None:
    assert classify(error) == (kind, retryable)


def test_every_kind_has_a_retry_decision() -> None:
    assert set(ERROR_KINDS) == {
        "forge_unavailable",
        "installation_removed",
        "pr_not_found",
        "diff_parse_failed",
        "provider_unavailable",
        "llm_output_invalid",
        "skip_diff_too_large",
        "internal_error",
    }
