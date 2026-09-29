import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from domain.errors import ProviderUnavailableError

Clock = Callable[[], float]

DEFAULT_COOLDOWN_SECONDS = 60.0
_MASK_VISIBLE_MIN_LENGTH = 12


def mask_key(key: str) -> str:
    """Render a key for logs and errors: 'sk-…abcd', never the full value."""
    if len(key) < _MASK_VISIBLE_MIN_LENGTH:
        return "…" + key[-2:]
    return f"{key[:3]}…{key[-4:]}"


@dataclass(slots=True)
class _KeyState:
    key: str
    cooldown_until: float = 0.0
    is_disabled: bool = False

    def is_available(self, now: float) -> bool:
        return not self.is_disabled and now >= self.cooldown_until


class KeyPool:
    """Round-robin over a provider's API keys with rate-limit cooldowns."""

    def __init__(self, provider: str, keys: Sequence[str], clock: Clock = time.monotonic) -> None:
        if not keys:
            raise ValueError("KeyPool needs at least one key")
        self._provider = provider
        self._states = [_KeyState(key) for key in dict.fromkeys(keys)]
        self._clock = clock
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._states)

    def acquire(self) -> str:
        now = self._clock()
        count = len(self._states)
        for offset in range(count):
            index = (self._cursor + offset) % count
            state = self._states[index]
            if state.is_available(now):
                self._cursor = (index + 1) % count
                return state.key
        raise ProviderUnavailableError(self._provider, "all API keys are rate-limited or rejected")

    def cool_down(self, key: str, seconds: float) -> None:
        self._state_of(key).cooldown_until = self._clock() + max(seconds, 0.0)

    def disable(self, key: str) -> None:
        self._state_of(key).is_disabled = True

    def _state_of(self, key: str) -> _KeyState:
        for state in self._states:
            if state.key == key:
                return state
        raise KeyError(mask_key(key))
