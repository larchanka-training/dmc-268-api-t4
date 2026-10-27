import threading
from functools import lru_cache

_hits = 0
_hits_lock = threading.Lock()


@lru_cache(maxsize=256)
def rate_for(currency: str) -> float:
    return _fetch_rate(currency)


def record_hit() -> int:
    global _hits
    with _hits_lock:
        _hits += 1
        return _hits


def _fetch_rate(currency: str) -> float:
    raise NotImplementedError
