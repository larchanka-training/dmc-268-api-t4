"""Graceful degradation (issue #88): a diff over the input budget is reviewed in part."""

import json
import logging
from collections.abc import Sequence

import pytest

from adapters.llm.config import ReviewScope
from adapters.llm.gateway import FallbackLLMGateway
from adapters.llm.prompt import build_messages, estimate_tokens
from adapters.llm.reflection import VerificationReport
from adapters.llm.rules import Rule
from domain.diff import select_files, split_named_files
from domain.errors import LLMInputTooLargeError
from domain.models import Finding, VerificationOutcome
from domain.ports import ReviewRequest
from tests.conftest import (
    RecordingTransport,
    all_log_text,
    chat_response,
    log_events,
    make_provider,
    replies,
    review_json,
)

CODE_SENTINEL = "CODE-SENTINEL-51c2"


def file_diff(path: str, lines: int, marker: str = "value") -> str:
    body = "".join(f"+{marker}_{n} = {n}\n" for n in range(lines))
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{lines} @@\n"
        f"{body}"
    )


SMALL, MEDIUM, LARGE = "app/small.py", "app/medium.py", "app/large.py"
TEST, DOC = "tests/test_large.py", "docs/notes.md"
DIFF = (
    file_diff(SMALL, 5, CODE_SENTINEL)
    + file_diff(LARGE, 120)
    + file_diff(TEST, 40)
    + file_diff(DOC, 30)
    + file_diff(MEDIUM, 60)
)


def request(diff: str = DIFF) -> ReviewRequest:
    return ReviewRequest(diff_text=diff, pr_title="Add modules", pr_description="")


def tokens(*paths: str) -> int:
    return estimate_tokens(build_messages(request(select_files(DIFF, set(paths)))))


class RecordingVerifier:
    def __init__(self) -> None:
        self.diffs: list[str] = []

    async def verify(
        self, request: ReviewRequest, rules: Sequence[Rule], findings: Sequence[Finding]
    ) -> VerificationReport:
        self.diffs.append(request.diff_text)
        return VerificationReport(VerificationOutcome.PASSED, tuple(findings), 0)


def gateway_with(
    limit: int,
    *,
    review_scope: ReviewScope = ReviewScope.PR,
    verifier: RecordingVerifier | None = None,
) -> tuple[FallbackLLMGateway, RecordingTransport]:
    transport = RecordingTransport(
        replies(*(chat_response(review_json(summary="Reviewed.")) for _ in range(8)))
    )
    gateway = FallbackLLMGateway(
        [make_provider(transport)],
        max_input_tokens=limit,
        review_scope=review_scope,
        verifier=verifier,
    )
    return gateway, transport


def sent_diff(transport: RecordingTransport, index: int = 0) -> str:
    return str(json.loads(transport.requests[index].content)["messages"][1]["content"])


def test_the_fixture_is_five_files() -> None:
    assert [path for path, _ in split_named_files(DIFF)] == [SMALL, LARGE, TEST, DOC, MEDIUM]


# --- 7: tests and docs are dropped first, then the largest files; the rest is reviewed ----


async def test_a_diff_within_the_budget_is_reviewed_whole() -> None:
    gateway, transport = gateway_with(tokens(SMALL, LARGE, TEST, DOC, MEDIUM))

    result = await gateway.review(request())

    assert result.not_reviewed == ()
    assert all(path in sent_diff(transport) for path in (SMALL, LARGE, TEST, DOC, MEDIUM))


async def test_the_test_file_is_dropped_first() -> None:
    gateway, transport = gateway_with(tokens(SMALL, LARGE, DOC, MEDIUM))

    result = await gateway.review(request())

    assert result.not_reviewed == (TEST,)
    assert result.summary == "Reviewed."
    sent = sent_diff(transport)
    assert TEST not in sent
    assert all(path in sent for path in (SMALL, LARGE, DOC, MEDIUM))


async def test_docs_go_after_tests_and_before_code() -> None:
    gateway, transport = gateway_with(tokens(SMALL, LARGE, MEDIUM))

    result = await gateway.review(request())

    assert result.not_reviewed == (TEST, DOC)
    assert LARGE in sent_diff(transport)


async def test_then_the_largest_remaining_file_goes() -> None:
    gateway, transport = gateway_with(tokens(SMALL, MEDIUM))

    result = await gateway.review(request())

    assert result.not_reviewed == (LARGE, TEST, DOC)
    sent = sent_diff(transport)
    assert SMALL in sent and MEDIUM in sent
    assert LARGE not in sent
    assert len(transport.requests) == 1


async def test_what_is_sent_never_exceeds_the_budget() -> None:
    limit = tokens(SMALL, MEDIUM)
    gateway, transport = gateway_with(limit)

    await gateway.review(request())

    messages = json.loads(transport.requests[0].content)["messages"]
    assert estimate_tokens(messages) <= limit


async def test_the_verifier_sees_the_reduced_request() -> None:
    verifier = RecordingVerifier()
    gateway, _ = gateway_with(tokens(SMALL, MEDIUM), verifier=verifier)

    result = await gateway.review(request())

    assert result.not_reviewed == (LARGE, TEST, DOC)
    assert LARGE not in verifier.diffs[0] and MEDIUM in verifier.diffs[0]


async def test_per_file_scope_leaves_out_only_the_files_that_do_not_fit_alone() -> None:
    gateway, transport = gateway_with(tokens(MEDIUM), review_scope=ReviewScope.FILE)

    result = await gateway.review(request())

    assert result.not_reviewed == (LARGE,)
    reviewed = "".join(sent_diff(transport, index) for index in range(4))
    assert LARGE not in reviewed
    assert all(path in reviewed for path in (SMALL, TEST, DOC, MEDIUM))


async def test_the_log_has_counts_only(caplog: pytest.LogCaptureFixture) -> None:
    limit = tokens(SMALL, MEDIUM)
    gateway, _ = gateway_with(limit)

    with caplog.at_level(logging.DEBUG):
        await gateway.review(request())

    [degraded] = log_events(caplog, "llm.input.degraded")
    assert (degraded.kept, degraded.dropped) == (2, 3)
    assert degraded.max_input_tokens == limit
    assert degraded.input_tokens > limit
    logged = all_log_text(caplog)
    assert CODE_SENTINEL not in logged
    assert LARGE not in logged


# --- 9: not a single file fits -> the review is refused as before ---------------------------


async def test_when_no_file_fits_the_review_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    gateway, transport = gateway_with(tokens(SMALL) - 1)

    with caplog.at_level(logging.WARNING), pytest.raises(LLMInputTooLargeError) as error:
        await gateway.review(request())

    assert transport.requests == []
    assert error.value.estimated_tokens == tokens(SMALL, LARGE, TEST, DOC, MEDIUM)
    assert len(log_events(caplog, "llm.input.too_large")) == 1
    assert log_events(caplog, "llm.input.degraded") == []


async def test_a_diff_that_cannot_be_split_by_file_is_refused() -> None:
    gateway, transport = gateway_with(1000)

    with pytest.raises(LLMInputTooLargeError):
        await gateway.review(request("x" * 30_000))

    assert transport.requests == []
