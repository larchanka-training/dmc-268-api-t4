"""adapters/observability/logging.py: what a JSON line keeps, and what it never carries.

Most tests format a hand-made LogRecord with ``build_formatter``, so no handler or
global logger is touched. Handler tests use a private logger, ``tmp_path`` and a
StringIO; the uvicorn test restores the global state through ``json_logs``. The
call-site test reads the source: every log call is a dotted event whose fields
are on the allowlist, enforced here rather than by review.
"""

import ast
import gzip
import io
import json
import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import pytest

from adapters.observability.config import LoggingSettings
from adapters.observability.logging import (
    ALLOWED_FIELDS,
    LOCAL_ONLY_FIELDS,
    MAX_VALUE_LENGTH,
    bind_context,
    build_formatter,
    configure_logging,
)
from domain.errors import ObservabilityConfigurationError
from tests.conftest import json_lines

PRODUCTION = LoggingSettings(environment="production", release="a" * 40)
LOCAL = LoggingSettings()
GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5" * 4
REPO_ROOT = Path(__file__).resolve().parents[2]


def render(
    settings: LoggingSettings,
    msg: str,
    *args: object,
    name: str = "worker.process",
    exc_info: Any = None,
    **fields: Any,
) -> dict[str, Any]:
    record = logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, exc_info)
    for key, value in fields.items():
        setattr(record, key, value)
    line: dict[str, Any] = json.loads(build_formatter(settings, "worker").format(record))
    return line


# --- settings ------------------------------------------------------------------


def test_settings_default_to_local_info_and_stdout_only() -> None:
    settings = LoggingSettings.from_env({})

    assert settings.is_local
    assert settings.level == "INFO"
    assert settings.log_dir is None
    assert settings.release is None


def test_settings_read_env_level_dir_and_image_tag() -> None:
    settings = LoggingSettings.from_env(
        {"ENV": "production", "LOG_LEVEL": "warning", "LOG_DIR": "/logs", "IMAGE_TAG": "abc"}
    )

    assert not settings.is_local
    assert settings.level == "WARNING"
    assert settings.level_number == logging.WARNING
    assert settings.log_dir == "/logs"
    assert settings.release == "abc"


def test_an_unknown_log_level_is_an_error_naming_the_variable() -> None:
    with pytest.raises(ObservabilityConfigurationError, match="LOG_LEVEL"):
        LoggingSettings.from_env({"LOG_LEVEL": "loud"})


# --- the line ------------------------------------------------------------------


def test_a_line_starts_with_the_mandatory_fields() -> None:
    line = render(PRODUCTION, "job.claimed")

    assert list(line)[:7] == ["ts", "level", "service", "env", "release", "logger", "event"]
    assert line["level"] == "info"
    assert (line["service"], line["env"], line["release"]) == ("worker", "production", "a" * 40)
    assert (line["logger"], line["event"]) == ("worker.process", "job.claimed")
    assert line["ts"].endswith("Z")


def test_our_loggers_keep_the_template_and_drop_the_arguments() -> None:
    line = render(PRODUCTION, "provider=%s reply rejected (%s)", "eurouter", "REPLY-MARKER")

    assert line["event"] == "provider=%s reply rejected (%s)"
    assert "msg" not in line
    assert "REPLY-MARKER" not in json.dumps(line)


def test_third_party_loggers_keep_their_message_redacted_and_cut() -> None:
    line = render(PRODUCTION, "token %s rejected after %s", GITHUB_TOKEN, "x" * 2000, name="httpx")

    assert line["event"] == "token %s rejected after %s"
    assert line["msg"].startswith("token [REDACTED:github_token] rejected")
    assert GITHUB_TOKEN not in json.dumps(line)
    assert len(line["msg"]) == MAX_VALUE_LENGTH


def test_the_webhook_logger_is_ours_whatever_its_name() -> None:
    line = render(PRODUCTION, "action=%s", "TITLE-MARKER", name="github_event")

    assert "TITLE-MARKER" not in json.dumps(line)


def test_allowlisted_fields_survive_in_production() -> None:
    line = render(
        PRODUCTION,
        "job.finished",
        job_id="j-1",
        outcome="COMPLETED",
        duration_ms=12,
        **{"http.status": 200},
    )

    assert (line["job_id"], line["outcome"], line["duration_ms"]) == ("j-1", "COMPLETED", 12)
    assert line["http.status"] == 200


def test_unknown_fields_are_dropped_in_production_and_kept_in_local() -> None:
    assert "body" not in render(PRODUCTION, "x.y", body="payload")
    assert render(LOCAL, "x.y", body="payload")["body"] == "payload"


@pytest.mark.parametrize("field", sorted(LOCAL_ONLY_FIELDS))
def test_prompt_reply_and_diff_fields_exist_only_in_local(field: str) -> None:
    assert field not in render(PRODUCTION, "x.y", **{field: "CONTENT"})
    assert render(LOCAL, "x.y", **{field: "CONTENT"})[field] == "CONTENT"


def test_every_string_field_is_redacted_and_cut() -> None:
    line = render(PRODUCTION, "x.y", reason=f"token {GITHUB_TOKEN}", repo="r" * 2000)

    assert line["reason"] == "token [REDACTED:github_token]"
    assert len(line["repo"]) == MAX_VALUE_LENGTH


class _Shape(StrEnum):
    SQUARE = "square"


class _Leaky:
    def __str__(self) -> str:
        return f"object holding {GITHUB_TOKEN}"


def test_values_that_are_not_json_become_redacted_strings() -> None:
    line = render(LOCAL, "x.y", shape=_Shape.SQUARE, thing=_Leaky())

    assert line["shape"] == "square"
    assert line["thing"] == "object holding [REDACTED:github_token]"


def test_bound_context_is_on_every_line_inside_the_block() -> None:
    with bind_context(job_id="j-9"):
        assert render(PRODUCTION, "x.y")["job_id"] == "j-9"
    assert "job_id" not in render(PRODUCTION, "x.y")


# --- exceptions --------------------------------------------------------------------


def _chained(*, explicit: bool) -> BaseException:
    try:
        try:
            raise KeyError("INNER-MARKER")
        except KeyError as inner:
            if explicit:
                raise RuntimeError("OUTER-MARKER") from inner
            raise RuntimeError("OUTER-MARKER")  # noqa: B904 - the implicit context is the point
    except RuntimeError as outer:
        return outer


def _exc_info(error: BaseException) -> tuple[type[BaseException], BaseException, Any]:
    return type(error), error, error.__traceback__


@pytest.mark.parametrize("explicit", [True, False])
def test_production_logs_each_chained_exception_without_its_message(explicit: bool) -> None:
    line = render(PRODUCTION, "job.store.failed", exc_info=_exc_info(_chained(explicit=explicit)))

    assert line["error_type"] == "RuntimeError"
    assert [link["type"] for link in line["exception"]] == ["RuntimeError", "KeyError"]
    assert all(link["frames"] for link in line["exception"])
    frame = line["exception"][0]["frames"][-1]
    assert set(frame) == {"file", "line", "function"}
    rendered = json.dumps(line)
    assert "OUTER-MARKER" not in rendered
    assert "INNER-MARKER" not in rendered


def test_a_suppressed_context_is_not_followed() -> None:
    try:
        try:
            raise KeyError("INNER-MARKER")
        except KeyError:
            raise RuntimeError("outer") from None
    except RuntimeError as error:
        line = render(PRODUCTION, "x.y", exc_info=_exc_info(error))

    assert [link["type"] for link in line["exception"]] == ["RuntimeError"]


def test_local_logs_keep_exception_messages() -> None:
    line = render(LOCAL, "x.y", exc_info=_exc_info(_chained(explicit=True)))

    assert [link["message"] for link in line["exception"]] == ["OUTER-MARKER", "'INNER-MARKER'"]


# --- handlers ------------------------------------------------------------------


@pytest.fixture
def private_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger(f"tests.observability.{uuid.uuid4().hex}")
    logger.propagate = False
    yield logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_lines_go_to_the_stream_as_json(private_logger: logging.Logger) -> None:
    stream = io.StringIO()
    configure_logging(PRODUCTION, "api", stream=stream, logger=private_logger)

    private_logger.info("job.claimed", extra={"job_id": "j-1"})

    [line] = json_lines(stream)
    assert (line["event"], line["job_id"], line["service"]) == ("job.claimed", "j-1", "api")


def test_configuring_again_replaces_only_its_own_handlers(private_logger: logging.Logger) -> None:
    foreign = logging.NullHandler()
    private_logger.addHandler(foreign)

    first = configure_logging(PRODUCTION, "api", stream=io.StringIO(), logger=private_logger)
    second = configure_logging(PRODUCTION, "api", stream=io.StringIO(), logger=private_logger)

    assert foreign in private_logger.handlers
    assert not any(handler in private_logger.handlers for handler in first)
    assert all(handler in private_logger.handlers for handler in second)


def _file_handler(handlers: list[logging.Handler]) -> TimedRotatingFileHandler:
    [handler] = [h for h in handlers if isinstance(h, TimedRotatingFileHandler)]
    return handler


def test_log_dir_gets_one_file_per_service_and_rotation_gzips_it(
    private_logger: logging.Logger, tmp_path: Path
) -> None:
    settings = LoggingSettings(environment="production", log_dir=str(tmp_path))
    handlers = configure_logging(settings, "api", stream=io.StringIO(), logger=private_logger)

    private_logger.info("job.claimed")
    handler = _file_handler(handlers)
    handler.flush()
    assert json.loads((tmp_path / "api.jsonl").read_text("utf-8"))["event"] == "job.claimed"

    handler.doRollover()

    [rotated] = list(tmp_path.glob("api.jsonl.*.gz"))
    assert re.fullmatch(r"api\.jsonl\.\d{4}-\d{2}-\d{2}\.gz", rotated.name)
    assert json.loads(gzip.decompress(rotated.read_bytes()))["event"] == "job.claimed"


def test_rotation_keeps_fourteen_files(private_logger: logging.Logger, tmp_path: Path) -> None:
    for day in range(1, 17):
        (tmp_path / f"api.jsonl.2026-01-{day:02d}.gz").write_bytes(gzip.compress(b"{}\n"))
    settings = LoggingSettings(environment="production", log_dir=str(tmp_path))
    handlers = configure_logging(settings, "api", stream=io.StringIO(), logger=private_logger)

    private_logger.info("job.claimed")
    _file_handler(handlers).doRollover()

    kept = sorted(path.name for path in tmp_path.glob("api.jsonl.*.gz"))
    assert len(kept) == 14
    assert "api.jsonl.2026-01-01.gz" not in kept


def test_an_unusable_log_dir_falls_back_to_stdout_with_a_warning(
    private_logger: logging.Logger, tmp_path: Path
) -> None:
    stream = io.StringIO()
    missing = tmp_path / "missing"
    settings = LoggingSettings(environment="production", log_dir=str(missing))

    handlers = configure_logging(settings, "api", stream=stream, logger=private_logger)

    assert len(handlers) == 1
    [line] = json_lines(stream)
    assert line["event"] == "logging.file_unavailable"
    assert (line["log_dir"], line["error_type"]) == (str(missing), "FileNotFoundError")


def test_uvicorn_logs_go_through_ours_and_its_access_log_is_off(json_logs: io.StringIO) -> None:
    assert logging.getLogger("uvicorn.access").disabled
    assert logging.getLogger("uvicorn").propagate

    logging.getLogger("uvicorn.error").info("Started server process [%d]", 42)

    [line] = [line for line in json_lines(json_logs) if line["logger"] == "uvicorn.error"]
    assert line["msg"] == "Started server process [42]"


# --- call sites and the spec -------------------------------------------------------

_SOURCE_ROOTS = ("adapters", "api", "worker")
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})
_LOGGER_NAMES = frozenset({"logger", "log", "_log", "target"})
_EVENT = re.compile(r"[a-z_]+(?:\.[a-z_]+)+")
_SPEC = REPO_ROOT / "docs" / "observability.md"
#: Shown with every failure: whoever reads it (a person or an agent) learns the fix and
#: the one wrong fix, putting a content field on the allowlist to make the test pass.
_HOW_TO_FIX = (
    "Log a dotted event name (job.finished) with its values in extra={...}, never %s "
    "arguments or an f-string. A new field goes on ALLOWED_FIELDS in "
    "adapters/observability/logging.py, unless it can hold content (diff, prompt, model "
    "reply, PR title or description, file content, a request or response body, a token): "
    "log a count or a length instead. See docs/observability.md § What is never logged."
)


@dataclass(frozen=True)
class _LogCall:
    where: str
    #: The message, when it is a string constant.
    event: str | None
    format_args: int
    fields: tuple[str, ...]


def _sources() -> list[Path]:
    paths = [path for root in _SOURCE_ROOTS for path in sorted((REPO_ROOT / root).rglob("*.py"))]
    return [*paths, REPO_ROOT / "main.py"]


def _dict_keys(
    node: ast.Dict, local: dict[str, ast.Dict], returns: dict[str, ast.Dict]
) -> list[str]:
    """String keys of a dict display, with ``**name`` and ``**call()`` splats followed
    to a dict display assigned or returned in the same module."""
    keys: list[str] = []
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.append(key.value)
            continue
        target = None
        if isinstance(value, ast.Name):
            target = local.get(value.id)
        elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            target = returns.get(value.func.id)
        keys += (
            _dict_keys(target, local, returns) if target else [f"<unresolved {ast.unparse(value)}>"]
        )
    return keys


def _log_calls(path: Path) -> list[_LogCall]:
    tree = ast.parse(path.read_text("utf-8"))
    local: dict[str, ast.Dict] = {}
    returns: dict[str, ast.Dict] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Dict):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(target, ast.Name):
                    local[target.id] = node.value
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Dict):
                    returns[node.name] = inner.value

    label = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else path.name
    calls = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        receiver = node.func.value
        name = receiver.attr if isinstance(receiver, ast.Attribute) else getattr(receiver, "id", "")
        if name not in _LOGGER_NAMES:
            continue
        if node.func.attr in _LOG_METHODS:
            args = node.args
        elif node.func.attr == "log":
            args = node.args[1:]
        else:
            continue
        message = args[0] if args else None
        fields = [
            key
            for keyword in node.keywords
            if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict)
            for key in _dict_keys(keyword.value, local, returns)
        ]
        calls.append(
            _LogCall(
                where=f"{label}:{node.lineno}",
                event=message.value if isinstance(message, ast.Constant) else None,
                format_args=max(len(args) - 1, 0),
                fields=tuple(fields),
            )
        )
    return calls


def _problems(call: _LogCall) -> list[str]:
    problems = []
    if not (isinstance(call.event, str) and _EVENT.fullmatch(call.event)):
        problems.append(f"{call.where}: the message is not a dotted event name (job.finished)")
    if call.format_args:
        problems.append(f"{call.where}: format arguments; pass the values as extra fields")
    allowed = ALLOWED_FIELDS | LOCAL_ONLY_FIELDS
    for key in call.fields:
        if key.startswith("<unresolved"):
            problems.append(
                f"{call.where}: extra spreads **{key[len('<unresolved ') : -1]}, which this "
                "check cannot follow; build the dict in the same module"
            )
        elif key not in allowed:
            problems.append(f"{call.where}: field {key!r} is not on ALLOWED_FIELDS")
    return problems


def test_every_log_call_is_a_dotted_event_with_allowlisted_fields() -> None:
    problems = [p for path in _sources() for call in _log_calls(path) for p in _problems(call)]

    assert problems == [], "\n".join([*problems, _HOW_TO_FIX])


def test_the_call_site_check_sees_a_bad_call(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import logging\nlogger = logging.getLogger(__name__)\n"
        'logger.info("job %s failed", 1, extra={"body": 2})\n',
        "utf-8",
    )

    [call] = _log_calls(bad)

    assert len(_problems(call)) == 3


def test_the_call_site_check_names_a_spread_it_cannot_follow(tmp_path: Path) -> None:
    opaque = tmp_path / "opaque.py"
    opaque.write_text(
        "import logging\nlogger = logging.getLogger(__name__)\n"
        'logger.info("job.done", extra={**fields_from_elsewhere()})\n',
        "utf-8",
    )

    [call] = _log_calls(opaque)

    [problem] = _problems(call)
    assert "extra spreads **fields_from_elsewhere()" in problem


def test_the_events_table_in_the_spec_matches_the_code() -> None:
    text = _SPEC.read_text("utf-8")
    section = text[text.index("### Events") : text.index("### What is never logged")]
    documented = set(re.findall(r"^\| `([a-z_.]+)` \|", section, re.MULTILINE))

    emitted = {call.event for path in _sources() for call in _log_calls(path)}

    assert documented - emitted == set(), (
        "in the Events table of docs/observability.md but never logged: remove the row, "
        "or rename it if the event was renamed"
    )
    assert emitted - documented == set(), (
        "logged but missing from the Events table of docs/observability.md: add a row "
        "(event, level, when it is logged, fields)"
    )
