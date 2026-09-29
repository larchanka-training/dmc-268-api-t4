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
