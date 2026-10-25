"""JSON logs for the API and the worker (docs/observability.md § Logs).

structlog renders stdlib records, so every ``logging.getLogger(...)`` call comes
out as one JSON line: to stdout, and to a rotated ``<LOG_DIR>/<service>.jsonl``
when ``LOG_DIR`` is set. Our code logs a stable dotted event name as the message
and its values as ``extra`` fields. Whatever a call site does, the processors
below keep content out of the line:

- our loggers' format arguments are dropped and only the template kept, so an
  interpolated value never reaches the line;
- outside ``local``, fields not on the allowlist are dropped;
- every string passes domain/redaction.py and is cut to 512 characters;
- an exception is its type and frames; its message appears only in ``local``.
"""

import contextlib
import gzip
import logging
import os
import shutil
import sys
import traceback
from collections.abc import Iterator, Mapping
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, TextIO

import sentry_sdk
import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from adapters.observability.config import LoggingSettings
from domain.redaction import redact

#: Loggers whose formatted message is kept, in ``msg``: they hold no diffs or
#: prompts, and without their arguments startup and connection errors would be
#: unreadable. Every other logger is ours, whatever its name (the webhook route
#: logs as ``github_event``).
THIRD_PARTY_LOGGERS = (
    "alembic",
    "asyncpg",
    "httpcore",
    "httpx",
    "sentry_sdk",
    "sqlalchemy",
    "uvicorn",
)

MAX_VALUE_LENGTH = 512

#: The keys a line may carry outside ``local``; anything else is dropped there.
#: tests/adapters/test_observability_logging.py fails when a call site passes an
#: ``extra`` key that is missing here.
ALLOWED_FIELDS = frozenset(
    {
        # every line
        "ts", "level", "logger", "event", "msg", "service", "env", "release",
        "trace_id", "span_id", "request_id",
        # review and request context
        "delivery_id", "job_id", "account_id", "installation_id", "repo_id", "repo",
        "pr_number", "head_sha", "base_sha", "attempt", "worker_id",
        "step", "outcome", "duration_ms",
        # errors
        "error_kind", "error_type", "exception", "reason",
        # access log
        "http.method", "http.route", "http.status",
        # LLM gateway
        "provider", "model", "input_tokens", "output_tokens", "max_input_tokens",
        "retries", "findings", "kept", "trimmed", "dropped", "cleared", "unanswered",
        "rules", "next_provider", "screen", "dropped_pattern", "dropped_screen",
        "dropped_over_limit", "key_masked", "cooldown_seconds",
        # sign-in audit
        "result", "user_id", "organizations", "had_session",
        # forge
        "sha", "commits", "dropped_commits", "diff_bytes", "pages", "count",
        "comments", "inline_fallback", "conclusion",
        # webhook
        "gh_event", "action",
        # job and worker
        "stage", "files", "reviewable", "hunks", "chunks", "reviewable_paths",
        "skipped_paths", "next_attempt_at", "channel", "reaped",
        # logging itself
        "log_dir",
    }
)  # fmt: skip

#: Kept only in ``local``: prompt tuning and the DEBUG_LOG_DIFF escape hatch.
LOCAL_ONLY_FIELDS = frozenset({"prompt", "reply", "diff_text"})

_FIRST_KEYS = ("ts", "level", "service", "env", "release", "logger", "event")
_BACKUP_DAYS = 14  # matches trace_retention_days
_OURS = "_dmc268_observability"


def configure_logging(
    settings: LoggingSettings,
    service: str,
    *,
    stream: TextIO | None = None,
    logger: logging.Logger | None = None,
) -> list[logging.Handler]:
    """Send ``logger`` (the root logger by default) to JSON on ``stream``
    (stdout by default) and, with ``LOG_DIR``, to the rotated file.

    Calling it again replaces only the handlers an earlier call installed, never
    anyone else's (pytest's caplog). A file that cannot be opened is reported
    and skipped: stdout still works, and a backup must not take the service down.
    """
    target = logger if logger is not None else logging.getLogger()
    formatter = build_formatter(settings, service)
    for handler in [h for h in target.handlers if getattr(h, _OURS, False)]:
        target.removeHandler(handler)
        handler.close()

    handlers: list[logging.Handler] = [
        logging.StreamHandler(stream if stream is not None else sys.stdout)
    ]
    file_error: OSError | None = None
    if settings.log_dir:
        try:
            handlers.append(_file_handler(Path(settings.log_dir), service))
        except OSError as error:
            file_error = error
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _OURS, True)
        target.addHandler(handler)
    target.setLevel(settings.level_number)

    if target is logging.getLogger():
        _route_uvicorn()
        structlog.configure(
            processors=[structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
        )
    if file_error is not None:
        target.warning(
            "logging.file_unavailable",
            extra={"log_dir": settings.log_dir, "error_type": type(file_error).__name__},
        )
    return handlers


def build_formatter(settings: LoggingSettings, service: str) -> logging.Formatter:
    local = settings.is_local
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _trace_ids,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        _static_fields(service, settings.environment, settings.release),
        _exception_fields(include_messages=local),
        _allowlist(local=local),
        _clean,
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.processors.JSONRenderer(),
    ]
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[_event_from_template, structlog.stdlib.ExtraAdder()],
        processors=processors,
    )


@contextlib.contextmanager
def bind_context(**fields: Any) -> Iterator[None]:
    """Add fields to every line logged inside the block (contextvars, so each
    request or job sees only its own). Sentry turns some into tags."""
    with structlog.contextvars.bound_contextvars(**fields):
        yield


def bind_fields(**fields: Any) -> None:
    """Add fields to every later line of the current context, without a block.
    Only for code that already runs in a context of its own, such as a request
    handler (each request runs in its own task), so nothing leaks to the next."""
    structlog.contextvars.bind_contextvars(**fields)


# --- processors --------------------------------------------------------------


def _is_third_party(name: str) -> bool:
    return any(name == root or name.startswith(root + ".") for root in THIRD_PARTY_LOGGERS)


def _event_from_template(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """The event is the record's template, never its formatted message: for our
    loggers the arguments are gone; third-party ones keep theirs in ``msg``."""
    record: logging.LogRecord = event_dict["_record"]
    formatted = event_dict["event"]
    event_dict["event"] = str(record.msg)
    if _is_third_party(record.name) and formatted != event_dict["event"]:
        event_dict["msg"] = formatted
    return event_dict


def _trace_ids(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """The active Sentry span's ids, so a line leads to its trace and back.
    Without Sentry, or outside a transaction, there is no span and no ids."""
    span = sentry_sdk.get_current_span()
    if span is not None:
        event_dict.setdefault("trace_id", span.trace_id)
        event_dict.setdefault("span_id", span.span_id)
    return event_dict


def _static_fields(service: str, env: str, release: str | None) -> Processor:
    def add(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("env", env)
        event_dict.setdefault("release", release)
        return event_dict

    return add


def _exception_fields(*, include_messages: bool) -> Processor:
    """``error_type`` plus the type and frames of every exception in the chain.

    A formatted traceback is not "frames only": its last line is ``Type: message``,
    and every chained exception repeats its own message. No local variables.
    """

    def render(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        exc_info = event_dict.pop("exc_info", None)
        event_dict.pop("stack_info", None)
        error = _as_exception(exc_info)
        if error is None:
            return event_dict
        event_dict.setdefault("error_type", type(error).__name__)
        event_dict["exception"] = [
            _describe(link, include_messages=include_messages) for link in _chain(error)
        ]
        return event_dict

    return render


def _as_exception(exc_info: object) -> BaseException | None:
    if exc_info is True:
        return sys.exc_info()[1]
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple) and len(exc_info) == 3:
        value = exc_info[1]
        return value if isinstance(value, BaseException) else None
    return None


def _chain(error: BaseException) -> Iterator[BaseException]:
    """The exception, then its cause or context, outermost first; cycles stop it."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )


def _describe(error: BaseException, *, include_messages: bool) -> dict[str, Any]:
    frames = [
        {"file": frame.filename, "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(error.__traceback__)
    ]
    described: dict[str, Any] = {"type": type(error).__name__, "frames": frames}
    if include_messages:
        described["message"] = str(error)
    return described


def _allowlist(*, local: bool) -> Processor:
    def keep(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        for key in list(event_dict):
            if key.startswith("_"):
                continue  # processor metadata, removed before rendering
            if key in LOCAL_ONLY_FIELDS:
                if not local:
                    del event_dict[key]
            elif not local and key not in ALLOWED_FIELDS:
                del event_dict[key]
        return event_dict

    return keep


def _clean(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Redact and cut every string; render anything not JSON-native as a string
    first, so it is redacted too. Puts the common keys first."""
    cleaned = {
        key: (value if key.startswith("_") else _clean_value(value))
        for key, value in event_dict.items()
    }
    ordered = {key: cleaned.pop(key) for key in _FIRST_KEYS if key in cleaned}
    ordered.update(cleaned)
    return ordered


def _clean_value(value: object) -> object:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {str(key): _clean_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_clean_value(item) for item in value]
    text = value if isinstance(value, str) else str(value)
    return redact(text)[0][:MAX_VALUE_LENGTH]


# --- handlers ----------------------------------------------------------------


def _file_handler(log_dir: Path, service: str) -> TimedRotatingFileHandler:
    """Daily at midnight UTC, 14 files kept, the closed file gzipped. One file per
    process: rotation in a file shared by processes loses lines."""
    handler = TimedRotatingFileHandler(
        log_dir / f"{service}.jsonl",
        when="midnight",
        utc=True,
        backupCount=_BACKUP_DAYS,
        encoding="utf-8",
    )
    handler.namer = _gzip_name
    handler.rotator = _gzip_rotate
    return handler


def _gzip_name(name: str) -> str:
    return name + ".gz"


def _gzip_rotate(source: str, destination: str) -> None:
    with open(source, "rb") as plain, gzip.open(destination, "wb") as packed:
        shutil.copyfileobj(plain, packed)
    os.remove(source)


def _route_uvicorn() -> None:
    """uvicorn configures its loggers before importing the app: send its server
    logs through ours, and silence its access log, which the API middleware
    replaces (it logs the route template, never the query string)."""
    for name in ("uvicorn", "uvicorn.error"):
        server = logging.getLogger(name)
        server.handlers.clear()
        server.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
