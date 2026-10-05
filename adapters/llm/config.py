import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from domain.errors import LLMConfigurationError

DEFAULT_PRIMARY_NAME = "eurouter"
DEFAULT_PRIMARY_BASE_URL = "https://api.eurouter.ai/api/v1"
DEFAULT_FALLBACK_NAME = "ollama"
DEFAULT_FALLBACK_BASE_URL = "http://localhost:11434/v1"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_TEMPERATURE = 0.0
# Providers that receive no max_tokens may reserve the whole context window for the reply
# and reject the request outright, so a reply budget is always sent.
DEFAULT_MAX_TOKENS = 4096
# Hard input budget, as recommended for qwen3-coder: a bigger prompt is refused before any
# provider is called. Fitting context into the budget is the context builder's job.
DEFAULT_MAX_INPUT_TOKENS = 32000
MAX_TEMPERATURE = 2.0

_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    name: str
    base_url: str
    model: str
    api_keys: tuple[str, ...] = field(default=(), repr=False)
    # Eurouter routing: try these upstream providers in order and never fall back to others.
    # Some upstreams reject requests for a model the catalog lists them for, so an
    # unpinned request fails whenever the router happens to pick one of them.
    provider_order: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LLMSettings:
    primary: ProviderSettings
    fallback: ProviderSettings | None
    timeout_seconds: float
    temperature: float
    json_mode: bool
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LLMSettings":
        return _EnvReader(os.environ if env is None else env).read_settings()


def parse_csv(raw: str) -> tuple[str, ...]:
    """Split a comma-separated list, dropping blanks and repeats but keeping order."""
    keys = (part.strip() for part in raw.split(","))
    return tuple(dict.fromkeys(key for key in keys if key))


class _EnvReader:
    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def read_settings(self) -> LLMSettings:
        fallback_enabled = self._bool("LLM_FALLBACK_ENABLED", default=False)
        required = ["LLM_PRIMARY_API_KEYS", "LLM_PRIMARY_MODEL"]
        if fallback_enabled:
            required.append("LLM_FALLBACK_MODEL")
        missing = [name for name in required if not self._text(name)]
        if missing:
            raise LLMConfigurationError(
                "missing required environment variable(s): " + ", ".join(missing)
            )

        primary_keys = parse_csv(self._text("LLM_PRIMARY_API_KEYS"))
        if not primary_keys:
            raise LLMConfigurationError(
                "LLM_PRIMARY_API_KEYS must contain at least one non-empty key"
            )

        primary = ProviderSettings(
            name=self._text("LLM_PRIMARY_NAME") or DEFAULT_PRIMARY_NAME,
            base_url=self._url("LLM_PRIMARY_BASE_URL", DEFAULT_PRIMARY_BASE_URL),
            model=self._text("LLM_PRIMARY_MODEL"),
            api_keys=primary_keys,
            provider_order=parse_csv(self._text("LLM_PRIMARY_PROVIDER_ORDER")),
        )
        fallback = None
        if fallback_enabled:
            fallback = ProviderSettings(
                name=self._text("LLM_FALLBACK_NAME") or DEFAULT_FALLBACK_NAME,
                base_url=self._url("LLM_FALLBACK_BASE_URL", DEFAULT_FALLBACK_BASE_URL),
                model=self._text("LLM_FALLBACK_MODEL"),
            )
            if fallback.name == primary.name:
                raise LLMConfigurationError(
                    "LLM_FALLBACK_NAME must differ from LLM_PRIMARY_NAME "
                    f"(both are '{primary.name}')"
                )

        return LLMSettings(
            primary=primary,
            fallback=fallback,
            timeout_seconds=self._float(
                "LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, minimum=0.0, exclusive=True
            ),
            temperature=self._float(
                "LLM_TEMPERATURE", DEFAULT_TEMPERATURE, minimum=0.0, maximum=MAX_TEMPERATURE
            ),
            json_mode=self._bool("LLM_JSON_MODE", default=True),
            max_tokens=self._positive_int("LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS),
            max_input_tokens=self._positive_int("LLM_MAX_INPUT_TOKENS", DEFAULT_MAX_INPUT_TOKENS),
        )

    def _text(self, name: str) -> str:
        return self._env.get(name, "").strip()

    def _url(self, name: str, default: str) -> str:
        url = (self._text(name) or default).rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise LLMConfigurationError(f"{name} must be an http(s) URL, got '{url}'")
        return url

    def _bool(self, name: str, *, default: bool) -> bool:
        raw = self._text(name).lower()
        if not raw:
            return default
        if raw in _TRUE_VALUES:
            return True
        if raw in _FALSE_VALUES:
            return False
        raise LLMConfigurationError(f"{name} must be true or false, got '{raw}'")

    def _positive_int(self, name: str, default: int) -> int:
        raw = self._text(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise LLMConfigurationError(f"{name} must be an integer, got '{raw}'") from None
        if value <= 0:
            raise LLMConfigurationError(f"{name} must be > 0, got {value}")
        return value

    def _float(
        self,
        name: str,
        default: float,
        *,
        minimum: float,
        maximum: float | None = None,
        exclusive: bool = False,
    ) -> float:
        raw = self._text(name)
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise LLMConfigurationError(f"{name} must be a number, got '{raw}'") from None
        below_minimum = value <= minimum if exclusive else value < minimum
        if below_minimum or (maximum is not None and value > maximum):
            bound = f"> {minimum}" if exclusive else f">= {minimum}"
            if maximum is not None:
                bound += f" and <= {maximum}"
            raise LLMConfigurationError(f"{name} must be {bound}, got {value}")
        return value
