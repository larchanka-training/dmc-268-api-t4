from datetime import UTC, datetime


class SystemClock:
    """The real clock. Everything else takes a Clock so tests need no sleep."""

    def now(self) -> datetime:
        return datetime.now(UTC)
