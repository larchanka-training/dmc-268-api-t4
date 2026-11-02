"""What the Sentry → Telegram alert relay forwards, and which alerts it drops.

docs/observability.md § Alerts, "The relay". The gate is noise control, not a security
boundary: the route checks the signature before anything here runs, and a replay that
gets through only costs a duplicate message. Pure: the caller passes the time.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

#: A body whose own event is older than this is a late delivery or a replay.
STALE_AFTER = timedelta(minutes=30)
#: How long an issue id + event id pair counts as already forwarded.
DUPLICATE_WINDOW = timedelta(hours=1)
#: Messages forwarded per RATE_WINDOW at most.
RATE_LIMIT = 30
RATE_WINDOW = timedelta(minutes=1)
#: The duplicate memory is bounded: the oldest pairs go first.
MAX_REMEMBERED = 1000


@dataclass(frozen=True, slots=True)
class Alert:
    """A Sentry issue alert, from a delivery whose signature was verified.

    ``title`` is the issue title, already redacted and clipped: in production it is the
    exception type and error_kind, never the exception message. ``occurred_at`` is None
    when the delivery carries no event time; such an alert is never stale.
    """

    issue_id: str
    event_id: str
    level: str
    title: str
    rule: str
    environment: str | None
    release: str | None
    link: str | None
    occurred_at: datetime | None


class DropReason(StrEnum):
    STALE = "stale"
    DUPLICATE = "duplicate"
    RATE_LIMITED = "rate_limited"


class RelayGate:
    """Decides whether an alert is forwarded. In memory: one API process (§ Metrics),
    and a restart forgets at most an hour of duplicates."""

    def __init__(self, *, max_remembered: int = MAX_REMEMBERED) -> None:
        self._forwarded: OrderedDict[tuple[str, str], datetime] = OrderedDict()
        self._recent: deque[datetime] = deque()
        self._max_remembered = max_remembered

    def admit(self, alert: Alert, now: datetime) -> DropReason | None:
        """None to forward the alert, or why it is dropped. Only a forwarded alert is
        remembered, so a dropped one does not make its later repeat a duplicate."""
        if alert.occurred_at is not None and now - alert.occurred_at > STALE_AFTER:
            return DropReason.STALE
        while self._forwarded and next(iter(self._forwarded.values())) <= now - DUPLICATE_WINDOW:
            self._forwarded.popitem(last=False)
        key = (alert.issue_id, alert.event_id)
        if key in self._forwarded:
            return DropReason.DUPLICATE
        while self._recent and self._recent[0] <= now - RATE_WINDOW:
            self._recent.popleft()
        if len(self._recent) >= RATE_LIMIT:
            return DropReason.RATE_LIMITED
        self._forwarded[key] = now
        if len(self._forwarded) > self._max_remembered:
            self._forwarded.popitem(last=False)
        self._recent.append(now)
        return None
