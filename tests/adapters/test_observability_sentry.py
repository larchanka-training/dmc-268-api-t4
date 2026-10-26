"""adapters/observability: Sentry settings, options, scrubbers, and what really
reaches a transport.

Unit tests call the scrubbers on hand-made events, or replace ``sentry_sdk.init``
with a recorder. The ``sentry_capture`` tests run the real SDK against a
transport that keeps envelopes in memory, so nothing reaches the network
(AGENTS.md hard rule 4); afterwards the client is replaced by a disabled one.
"""

import io
import json
import logging
from typing import Any

import httpx
import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from adapters.jobs.memory import MemoryJobStore
from adapters.observability import sentry as sentry_module
from adapters.observability.config import SentrySettings
from adapters.observability.logging import bind_context
from adapters.observability.sentry import (
    init_sentry,
    scrub_breadcrumb,
    scrub_event,
    traces_sampler,
)
from domain.errors import ForgeUnavailableError, InstallationGoneError
from tests.conftest import (
    TEST_ORIGIN,
    CaptureTransport,
    FakeIdentityProvider,
    FixedClock,
    build_test_app,
    json_lines,
)
from tests.worker.test_process import FakeGitProvider, claim, enqueue, make_payload
from worker.process import process_claimed_job

FAKE_DSN = "https://public@o0.ingest.sentry.example/0"
GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5" * 4
PRODUCTION = SentrySettings(dsn=FAKE_DSN, environment="production", release="r1")


@pytest.fixture
def recorded_init(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    return calls


# --- settings and options ---------------------------------------------------------


def test_settings_default_to_off_and_local() -> None:
    settings = SentrySettings.from_env({})

    assert not settings.enabled
    assert settings.is_local
    assert settings.release is None


def test_settings_read_the_dsn_env_and_image_tag() -> None:
    settings = SentrySettings.from_env(
        {"SENTRY_DSN": f" {FAKE_DSN} ", "ENV": "production", "IMAGE_TAG": "a" * 40}
    )

    assert settings.enabled
    assert settings.dsn == FAKE_DSN
    assert not settings.is_local
    assert settings.release == "a" * 40


def test_an_empty_dsn_is_off() -> None:
    assert not SentrySettings.from_env({"SENTRY_DSN": "  "}).enabled


def test_the_dsn_stays_out_of_the_repr() -> None:
    assert FAKE_DSN not in repr(SentrySettings(dsn=FAKE_DSN))


def test_init_without_a_dsn_does_nothing(recorded_init: list[dict[str, Any]]) -> None:
    assert init_sentry(SentrySettings()) is False
    assert recorded_init == []


def test_init_turns_off_every_default_that_leaks_data(
    recorded_init: list[dict[str, Any]],
) -> None:
    assert init_sentry(PRODUCTION) is True

    [options] = recorded_init
    assert (options["dsn"], options["environment"], options["release"]) == (
        FAKE_DSN,
        "production",
        "r1",
    )
    # Unset is the SDK's no-PII default (send_default_pii is deprecated in 2.73).
    assert "send_default_pii" not in options
    assert "data_collection" not in options
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["traces_sampler"] is traces_sampler
    assert options["trace_propagation_targets"] == []
    for hook in ("before_send", "before_send_transaction", "before_breadcrumb"):
        assert callable(options[hook])


def test_only_our_integrations_are_on_so_no_llm_sdk_integration_can_be(
    recorded_init: list[dict[str, Any]],
) -> None:
    init_sentry(PRODUCTION)

    [options] = recorded_init
    assert options["auto_enabling_integrations"] is False
    assert {type(i) for i in options["integrations"]} == {
        StarletteIntegration,
        FastApiIntegration,
        HttpxIntegration,
        SqlalchemyIntegration,
    }


@pytest.mark.parametrize(
    ("context", "rate"),
    [
        ({"asgi_scope": {"path": "/health", "root_path": ""}}, 0.0),
        ({"asgi_scope": {"path": "/api/health", "root_path": "/api"}}, 0.0),
        ({"asgi_scope": {"path": "/api/jobs/0192f1c4/events", "root_path": "/api"}}, 0.0),
        ({"asgi_scope": {"path": "/api/jobs/0192f1c4", "root_path": "/api"}}, 1.0),
        ({"asgi_scope": {"path": "/api/webhooks/github", "root_path": "/api"}}, 1.0),
        # Worker transactions have no ASGI scope.
        ({}, 1.0),
    ],
)
def test_the_healthcheck_and_the_sse_stream_are_not_traced(
    context: dict[str, Any], rate: float
) -> None:
    assert traces_sampler(context) == rate


# --- before_send -------------------------------------------------------------------


def _event(**fields: Any) -> Any:
    return {
        "request": {
            "url": "https://example.test/api/auth/github/callback",
            "query_string": "code=gho-test-code&state=state-1",
            "data": '{"pr_title": "secret"}',
            "cookies": {"__Host-session": "s"},
            "headers": {
                "User-Agent": "GitHub-Hookshot/1",
                "X-Hub-Signature-256": "sha256=abc",
                "X-GitHub-Delivery": "d-1",
            },
        },
        "exception": {
            "values": [
                {"type": "KeyError", "value": "INNER-MESSAGE", "stacktrace": {"frames": []}},
                {"type": "RuntimeError", "value": "OUTER-MESSAGE", "stacktrace": {"frames": []}},
            ]
        },
        "logentry": {"message": "job.finished", "params": ["p"], "formatted": "job.finished p"},
        "extra": {"error_kind": "installation_removed", "job_id": "j-1", "body": "payload"},
        **fields,
    }


def test_the_request_keeps_its_url_and_allowlisted_headers_only() -> None:
    event = scrub_event(production=False)(_event(), {})

    assert event is not None
    request = event["request"]
    assert request["url"] == "https://example.test/api/auth/github/callback"
    assert set(request) == {"url", "headers"}
    assert request["headers"] == {"User-Agent": "GitHub-Hookshot/1", "X-GitHub-Delivery": "d-1"}


def test_production_replaces_every_exception_message_with_the_error_kind() -> None:
    event = scrub_event(production=True)(_event(), {})

    assert event is not None
    values = event["exception"]["values"]
    assert [v["type"] for v in values] == ["KeyError", "RuntimeError"]
    assert [v["value"] for v in values] == ["installation_removed", "installation_removed"]
    assert "MESSAGE" not in json.dumps(event)


def test_production_drops_the_formatted_message_and_unlisted_extra() -> None:
    event = scrub_event(production=True)(_event(), {})

    assert event is not None
    assert event["logentry"] == {"message": "job.finished"}
    assert event["extra"] == {"error_kind": "installation_removed", "job_id": "j-1"}


def test_local_keeps_messages_and_extra() -> None:
    event = scrub_event(production=False)(_event(), {})

    assert event is not None
    assert event["exception"]["values"][1]["value"] == "OUTER-MESSAGE"
    assert event["extra"]["body"] == "payload"


def test_bound_context_and_log_fields_become_tags() -> None:
    with bind_context(job_id="j-9", installation_id=42, request_id="r-1"):
        event = scrub_event(production=True)(_event(), {})

    assert event is not None
    assert event["tags"] == {
        "installation_id": "42",
        "job_id": "j-9",
        "request_id": "r-1",
        "error_kind": "installation_removed",
    }


def test_every_string_is_redacted() -> None:
    event = scrub_event(production=False)({"message": f"token {GITHUB_TOKEN}"}, {})

    assert event is not None
    assert event["message"] == "token [REDACTED:github_token]"


# --- before_breadcrumb --------------------------------------------------------------


def _log_crumb() -> tuple[dict[str, Any], dict[str, Any]]:
    record = logging.LogRecord("worker.process", logging.INFO, __file__, 1, "job.files", (), None)
    crumb = {"type": "log", "category": "worker.process", "message": "job.files", "data": {"x": 1}}
    return crumb, {"log_record": record}


def test_production_logging_breadcrumbs_keep_the_event_name_only() -> None:
    crumb, hint = _log_crumb()

    scrubbed = scrub_breadcrumb(production=True)(crumb, hint)

    assert scrubbed is not None
    assert scrubbed["message"] == "job.files"
    assert "data" not in scrubbed
    assert scrubbed["category"] == "worker.process"


def test_local_logging_breadcrumbs_keep_their_data() -> None:
    crumb, hint = _log_crumb()

    scrubbed = scrub_breadcrumb(production=False)(crumb, hint)

    assert scrubbed is not None
    assert scrubbed["data"] == {"x": 1}


def test_other_breadcrumbs_are_redacted() -> None:
    crumb = {"type": "http", "data": {"url": f"https://x.example/?t={GITHUB_TOKEN}"}}

    scrubbed = scrub_breadcrumb(production=True)(crumb, {})

    assert scrubbed is not None
    assert GITHUB_TOKEN not in json.dumps(scrubbed)


# --- the real SDK -------------------------------------------------------------------


async def test_a_failed_review_is_one_event_tagged_with_its_error_kind(
    sentry_capture: CaptureTransport,
) -> None:
    store = MemoryJobStore()
    job_id = await enqueue(store, make_payload())
    gone = FakeGitProvider(error=InstallationGoneError(42, "permission revoked SECRET-DETAIL"))

    assert await process_claimed_job(await claim(store), gone, store) is False
    sentry_sdk.flush()

    [event] = sentry_capture.payloads("event")
    assert event["tags"]["job_id"] == job_id
    assert event["tags"]["error_kind"] == "installation_removed"
    assert event["exception"]["values"][-1]["type"] == "InstallationGoneError"
    assert event["exception"]["values"][-1]["value"] == "installation_removed"
    assert "SECRET-DETAIL" not in sentry_capture.dump()


async def test_a_retried_review_is_no_event(sentry_capture: CaptureTransport) -> None:
    store = MemoryJobStore()
    await enqueue(store, make_payload())
    down = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset"))

    assert await process_claimed_job(await claim(store), down, store) is False
    sentry_sdk.flush()

    assert sentry_capture.payloads("event") == []


def test_production_logging_breadcrumbs_reach_events_without_values(
    sentry_capture: CaptureTransport,
) -> None:
    logging.getLogger("worker.process").warning("job.files", extra={"repo": "acme/SECRET-REPO"})
    sentry_sdk.capture_message("boom")
    sentry_sdk.flush()

    [event] = sentry_capture.payloads("event")
    crumbs = event["breadcrumbs"]["values"]
    assert crumbs[-1]["message"] == "job.files"
    assert "SECRET-REPO" not in sentry_capture.dump()


async def test_outgoing_calls_carry_no_trace_headers_but_keep_their_span(
    sentry_capture: CaptureTransport,
) -> None:
    sent: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with sentry_sdk.start_transaction(name="job.process", op="queue.task", sampled=True):
            await client.get("https://api.github.com/repos/acme/app/pulls/1")
    sentry_sdk.flush()

    [headers] = sent
    assert "sentry-trace" not in headers
    assert "baggage" not in headers
    [transaction] = sentry_capture.payloads("transaction")
    assert [span["op"] for span in transaction["spans"]] == ["http.client"]


def test_api_log_lines_carry_the_request_trace_ids(
    sentry_capture: CaptureTransport, json_logs: io.StringIO, auth_clock: FixedClock
) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())
    with TestClient(app, base_url=TEST_ORIGIN) as client:
        client.get("/github/setup", follow_redirects=False)
    sentry_sdk.flush()

    [line] = [line for line in json_lines(json_logs) if line["event"] == "github.setup.redirect"]
    [transaction] = sentry_capture.payloads("transaction")
    assert line["trace_id"] == transaction["contexts"]["trace"]["trace_id"]
    assert line["span_id"]
