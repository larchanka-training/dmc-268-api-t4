from collections.abc import Sequence


class LLMGatewayError(Exception):
    """Base class for every error the LLM gateway raises to its callers."""


class LLMConfigurationError(LLMGatewayError):
    """The gateway cannot be built from the given settings."""


class ProviderUnavailableError(LLMGatewayError):
    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"LLM provider '{provider}' unavailable: {reason}")
        self.provider = provider
        self.reason = reason


class LLMOutputError(LLMGatewayError):
    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"LLM provider '{provider}' returned unusable output: {reason}")
        self.provider = provider
        self.reason = reason


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
