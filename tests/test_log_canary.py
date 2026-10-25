"""The canary test of docs/observability.md § What is never logged.

One marker is planted in a PR's diff, title and description, in the model's
replies and in the providers' error bodies. Then the failure paths run: invalid
JSON, a schema mismatch, a provider error, all providers failed, a webhook
delivery, and a worker run with DEBUG_LOG_DIFF=1 outside ``local``. The marker
must not appear anywhere in the production JSON output, nor in the Sentry events,
breadcrumbs and transactions a test transport captures. Everything runs on fakes
and ``httpx.MockTransport``; nothing touches the network.
"""

import dataclasses
import io
import json

import httpx
import pytest
import sentry_sdk

from adapters.jobs.memory import MemoryJobStore
from adapters.llm.gateway import FallbackLLMGateway
from domain.errors import AllProvidersFailedError, InstallationGoneError
from domain.ports import ReviewRequest
from tests.api.test_webhook_github import HeldJobStore, opened_payload, post_delivery
from tests.conftest import (
    VALID_FINDING,
    CaptureTransport,
    FakeIdentityProvider,
    FixedClock,
    RecordingTransport,
    build_test_app,
    chat_response,
    json_lines,
    make_provider,
    replies,
    review_json,
)
from tests.worker.test_process import GOOD_DIFF, FakeGitProvider, claim, enqueue, make_context
from tests.worker.test_process import make_payload as make_job_payload
from worker.process import ReviewStage, process_claimed_job

MARKER = "CANARY-7f3e-content-that-must-never-be-logged"
REQUEST = ReviewRequest(
    diff_text=f"diff --git a/x b/x\n+print('{MARKER}')\n",
    pr_title=f"title {MARKER}",
    pr_description=f"description {MARKER}",
)


def assert_marker_absent(stream: io.StringIO) -> None:
    output = stream.getvalue()
    assert output, "nothing was logged, so nothing was checked"
    assert all(json.loads(line) for line in output.splitlines())
    assert MARKER not in output


async def test_invalid_json_replies_never_reach_the_logs(json_logs: io.StringIO) -> None:
    transport = RecordingTransport(
        replies(chat_response(f"not json {MARKER}"), chat_response(f"still not {MARKER}"))
    )

    with pytest.raises(AllProvidersFailedError):
        await FallbackLLMGateway([make_provider(transport)]).review(REQUEST)

    assert_marker_absent(json_logs)


async def test_schema_mismatches_never_reach_the_logs(json_logs: io.StringIO) -> None:
    bad_envelope = json.dumps({"summary": MARKER, "findings": MARKER})
    bad_finding = {**VALID_FINDING, "severity": "urgent", "message": MARKER}
    transport = RecordingTransport(
        replies(
            chat_response(bad_envelope),
            chat_response(bad_envelope),
        )
    )
    with pytest.raises(AllProvidersFailedError):
        await FallbackLLMGateway([make_provider(transport)]).review(REQUEST)

    partly_valid = RecordingTransport(replies(chat_response(review_json(bad_finding))))
    await FallbackLLMGateway([make_provider(partly_valid)]).review(REQUEST)

    assert_marker_absent(json_logs)


async def test_provider_error_bodies_never_reach_the_logs(json_logs: io.StringIO) -> None:
    # A 4xx body is kept in the exception (ProviderUnavailableError.detail) and may
    # echo the request; a 5xx body is not read at all. Both providers fail.
    primary = make_provider(
        RecordingTransport(replies(httpx.Response(400, text=f"bad request: {MARKER}")))
    )
    fallback = make_provider(
        RecordingTransport(replies(httpx.Response(500, text=MARKER))), name="ollama", keys=()
    )

    with pytest.raises(AllProvidersFailedError) as excinfo:
        await FallbackLLMGateway([primary, fallback]).review(REQUEST)

    assert MARKER in str(excinfo.value)  # the caller still sees why
    lines = json_lines(json_logs)
    assert {"llm.provider.fallback", "llm.call.failed"} <= {line["event"] for line in lines}
    assert_marker_absent(json_logs)


async def test_a_webhook_delivery_never_logs_the_title(
    auth_clock: FixedClock, json_logs: io.StringIO
) -> None:
    payload = opened_payload()
    payload["pull_request"]["title"] = f"title {MARKER}"
    payload["pull_request"]["body"] = f"description {MARKER}"
    app = build_test_app(auth_clock, FakeIdentityProvider(), jobs=HeldJobStore(auth_clock))

    response = await post_delivery(app, json.dumps(payload).encode("utf-8"))

    assert response.status_code == 202
    assert "webhook.accepted" in {line["event"] for line in json_lines(json_logs)}
    assert_marker_absent(json_logs)


async def test_a_failing_review_run_never_logs_content(
    json_logs: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DEBUG_LOG_DIFF is the one place that logs a diff, and only in ENV=local.
    monkeypatch.setenv("DEBUG_LOG_DIFF", "1")
    monkeypatch.setenv("ENV", "production")
    context = dataclasses.replace(
        make_context(GOOD_DIFF.replace("+more", f"+{MARKER}")),
        title=f"title {MARKER}",
        description=f"description {MARKER}",
    )
    assert MARKER in context.diff_text
    failing = make_provider(
        RecordingTransport(replies(httpx.Response(400, text=f"echo: {MARKER}")))
    )
    review = ReviewStage(gateway=FallbackLLMGateway([failing]), publisher=object())  # type: ignore[arg-type]
    store = MemoryJobStore()
    await enqueue(store, make_job_payload())
    claimed = await claim(store)

    assert (
        await process_claimed_job(claimed, FakeGitProvider(context), store, review=review) is False
    )

    events = {line["event"] for line in json_lines(json_logs)}
    assert {"job.files", "llm.call.failed", "job.retrying"} <= events
    assert "job.diff.debug" not in events
    assert_marker_absent(json_logs)


async def test_sentry_events_and_breadcrumbs_never_carry_content(
    auth_clock: FixedClock,
    json_logs: io.StringIO,
    sentry_capture: CaptureTransport,
) -> None:
    # Breadcrumbs: a provider failure, and a webhook delivery with the marker in its title.
    failing = make_provider(RecordingTransport(replies(httpx.Response(400, text=MARKER))))
    with pytest.raises(AllProvidersFailedError):
        await FallbackLLMGateway([failing]).review(REQUEST)
    payload = opened_payload()
    payload["pull_request"]["title"] = f"title {MARKER}"
    app = build_test_app(auth_clock, FakeIdentityProvider(), jobs=HeldJobStore(auth_clock))
    assert (await post_delivery(app, json.dumps(payload).encode("utf-8"))).status_code == 202

    # Events: a review that fails for good, and an exception with the marker as its message.
    context = dataclasses.replace(
        make_context(GOOD_DIFF.replace("+more", f"+{MARKER}")), title=f"title {MARKER}"
    )
    gone = FakeGitProvider(context, error=InstallationGoneError(42, f"revoked {MARKER}"))
    store = MemoryJobStore()
    await enqueue(store, make_job_payload())
    assert await process_claimed_job(await claim(store), gone, store) is False
    sentry_sdk.capture_exception(RuntimeError(MARKER))
    sentry_sdk.flush()

    events = sentry_capture.payloads("event")
    assert len(events) == 2
    assert any(event.get("breadcrumbs", {}).get("values") for event in events)
    assert MARKER not in sentry_capture.dump()
