import json

from app.prefs.models import Preferences

_STORE: dict[int, str] = {}


def save(prefs: Preferences) -> None:
    _STORE[prefs.user_id] = json.dumps(prefs.__dict__)


def load(user_id: int) -> Preferences:
    raw = _STORE.get(user_id)
    if raw is None:
        return Preferences(user_id)
    data = json.loads(raw)
    data["quiet_hours"] = tuple(data["quiet_hours"])
    return Preferences(**data)


def is_quiet(prefs: Preferences, hour: int) -> bool:
    start, end = prefs.quiet_hours
    return start <= hour or hour < end
