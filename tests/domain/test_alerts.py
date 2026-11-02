"""domain/alerts.py: which Sentry alerts the relay forwards (docs/observability.md §
Alerts, "The relay")."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from domain.alerts import (
    DUPLICATE_WINDOW,
    RATE_LIMIT,
    STALE_AFTER,
    Alert,
    DropReason,
    RelayGate,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
ALERT = Alert(
    issue_id="1117540176",
    event_id="e4874d664c3540c1a32eab185f12c5ab",
    level="error",
    title="ForgeUnavailableError: forge_unavailable",
    rule="New issue",
    environment="production",
    release="a" * 40,
    link="https://sentry.example/issues/1117540176/",
    occurred_at=NOW,
)


def other(index: int) -> Alert:
    return replace(ALERT, event_id=f"event-{index}")


def test_a_fresh_alert_is_forwarded() -> None:
    assert RelayGate().admit(ALERT, NOW) is None


def test_an_alert_older_than_thirty_minutes_is_stale() -> None:
    gate = RelayGate()

    assert gate.admit(ALERT, NOW + STALE_AFTER) is None
    assert gate.admit(other(1), NOW + STALE_AFTER + timedelta(seconds=1)) is DropReason.STALE


def test_an_alert_without_an_event_time_is_never_stale() -> None:
    late = replace(ALERT, occurred_at=None)

    assert RelayGate().admit(late, NOW + timedelta(days=1)) is None


def test_the_same_issue_and_event_within_an_hour_is_a_duplicate() -> None:
    gate = RelayGate()
    gate.admit(ALERT, NOW)

    assert gate.admit(ALERT, NOW + timedelta(minutes=5)) is DropReason.DUPLICATE
    # Another event of the same issue is a new alert.
    assert gate.admit(other(1), NOW + timedelta(minutes=5)) is None


def test_a_repeat_after_the_window_is_forwarded_again() -> None:
    gate = RelayGate()
    gate.admit(replace(ALERT, occurred_at=None), NOW)

    later = NOW + DUPLICATE_WINDOW + timedelta(seconds=1)
    assert gate.admit(replace(ALERT, occurred_at=None), later) is None


def test_more_than_thirty_a_minute_are_rate_limited() -> None:
    gate = RelayGate()
    for index in range(RATE_LIMIT):
        assert gate.admit(other(index), NOW + timedelta(seconds=index)) is None

    assert gate.admit(other(RATE_LIMIT), NOW + timedelta(seconds=59)) is DropReason.RATE_LIMITED
    # A minute after the first one, there is room again.
    assert gate.admit(other(RATE_LIMIT), NOW + timedelta(seconds=60)) is None


def test_a_dropped_alert_is_not_remembered_as_forwarded() -> None:
    gate = RelayGate()
    for index in range(RATE_LIMIT):
        gate.admit(other(index), NOW)
    assert gate.admit(ALERT, NOW) is DropReason.RATE_LIMITED

    assert gate.admit(ALERT, NOW + timedelta(minutes=1)) is None


def test_the_duplicate_memory_is_bounded() -> None:
    gate = RelayGate(max_remembered=2)
    for index, at in enumerate((NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=2))):
        gate.admit(other(index), at)

    # The oldest pair was forgotten, so its repeat is forwarded; the newest is not.
    assert gate.admit(other(0), NOW + timedelta(minutes=3)) is None
    assert gate.admit(other(2), NOW + timedelta(minutes=3)) is DropReason.DUPLICATE
