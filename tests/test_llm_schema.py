import logging

import pytest

from adapters.llm.gateway import review_with_provider
from adapters.llm.schema import (
    FINDINGS_MAX,
    PATH_MAX_LENGTH,
    SUGGESTION_MAX_LINES,
    SUMMARY_MAX_LENGTH,
    InvalidReviewJSONError,
    parse_review,
    strip_code_fence,
)
from domain.errors import LLMOutputError
from domain.models import (
    Category,
    DiffSide,
    Finding,
    FindingPosition,
    FindingSuggestion,
    Severity,
)
from domain.ports import ReviewRequest
from tests.conftest import (
    VALID_FINDING,
    RecordingTransport,
    chat_response,
    make_provider,
    replies,
    review_json,
)

REQUEST = ReviewRequest(diff_text="diff --git a/x b/x", pr_title="t", pr_description="d")


def test_valid_json_maps_to_domain_findings() -> None:
    parsed = parse_review(review_json(VALID_FINDING))

    assert parsed.dropped_findings == 0
    assert parsed.review.summary == "Adds a users endpoint."
    assert parsed.review.to_domain() == (
        Finding(
            position=FindingPosition(path="app/users.py", line=16, side=DiffSide.NEW),
            severity=Severity.CRITICAL,
            category=Category.SECURITY,
            message=VALID_FINDING["message"],
            suggestion=FindingSuggestion(
                before=tuple(VALID_FINDING["suggestion"]["before"]),
                after=tuple(VALID_FINDING["suggestion"]["after"]),
            ),
        ),
    )


@pytest.mark.parametrize("language", ["json", ""])
def test_fenced_reply_parses_like_plain_json(language: str) -> None:
    plain = review_json(VALID_FINDING)
    fenced = f"```{language}\n{plain}\n```\n"

    assert strip_code_fence(fenced) == plain
    assert parse_review(fenced) == parse_review(plain)


def test_unknown_severity_drops_only_that_finding() -> None:
    urgent = {**VALID_FINDING, "severity": "urgent"}

    parsed = parse_review(review_json(urgent, VALID_FINDING))

    assert parsed.dropped_findings == 1
    assert [f.severity for f in parsed.review.to_domain()] == [Severity.CRITICAL]


@pytest.mark.parametrize(
    "override",
    [
        {"category": "readability"},
        {"severity": "High"},
        {"side": "right"},
    ],
)
def test_near_miss_enum_values_are_not_coerced(override: dict[str, str]) -> None:
    parsed = parse_review(review_json({**VALID_FINDING, **override}))

    assert parsed.dropped_findings == 1
    assert parsed.review.findings == []


@pytest.mark.parametrize(
    "override",
    [
        {"line": 0},
        {"line": -3},
        {"message": ""},
        {"message": "   "},
        {"message": "x" * 2001},
        {"path": ""},
    ],
)
def test_invalid_position_or_message_drops_finding(override: dict[str, object]) -> None:
    parsed = parse_review(review_json({**VALID_FINDING, **override}, VALID_FINDING))

    assert parsed.dropped_findings == 1
    assert len(parsed.review.findings) == 1


def test_null_suggestion_becomes_none() -> None:
    parsed = parse_review(review_json({**VALID_FINDING, "suggestion": None}))

    (finding,) = parsed.review.to_domain()
    assert finding.suggestion is None


def test_missing_suggestion_becomes_none() -> None:
    without_suggestion = {k: v for k, v in VALID_FINDING.items() if k != "suggestion"}

    (finding,) = parse_review(review_json(without_suggestion)).review.to_domain()

    assert finding.suggestion is None


@pytest.mark.parametrize(
    "reply",
    ["not json", "", "[]", '{"summary": "no findings key"}', '{"summary": 1, "findings": []}'],
)
def test_reply_without_review_object_is_invalid(reply: str) -> None:
    with pytest.raises(InvalidReviewJSONError):
        parse_review(reply)


async def test_invalid_json_then_valid_retries_once() -> None:
    transport = RecordingTransport(
        replies(chat_response("Sure! Here is my review."), chat_response(review_json()))
    )

    result = await review_with_provider(make_provider(transport), REQUEST)

    assert result.findings == ()
    assert len(transport.requests) == 2
    retry_messages = transport.requests[1].read().decode()
    assert "Return only the JSON object" in retry_messages


async def test_invalid_json_twice_raises_output_error() -> None:
    transport = RecordingTransport(replies(chat_response("nope"), chat_response("still nope")))

    with pytest.raises(LLMOutputError, match="eurouter"):
        await review_with_provider(make_provider(transport), REQUEST)
    assert len(transport.requests) == 2


async def test_dropped_findings_are_counted_in_warning_without_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = {**VALID_FINDING, "severity": "urgent", "message": "SECRET-FINDING-TEXT"}
    transport = RecordingTransport(replies(chat_response(review_json(bad, VALID_FINDING))))

    with caplog.at_level(logging.INFO, logger="adapters.llm"):
        result = await review_with_provider(make_provider(transport), REQUEST)

    assert len(result.findings) == 1
    assert "dropped 1 finding(s)" in caplog.text
    assert "SECRET-FINDING-TEXT" not in caplog.text
    assert VALID_FINDING["message"] not in caplog.text


def test_summary_is_cut_to_the_schema_limit() -> None:
    parsed = parse_review(review_json(VALID_FINDING, summary="s" * (SUMMARY_MAX_LENGTH + 500)))

    assert len(parsed.review.summary) == SUMMARY_MAX_LENGTH


def test_too_long_path_drops_finding() -> None:
    long_path = {**VALID_FINDING, "path": "a/" * PATH_MAX_LENGTH}

    parsed = parse_review(review_json(long_path, VALID_FINDING))

    assert len(parsed.review.findings) == 1
    assert parsed.dropped_findings == 1


def test_too_long_suggestion_drops_finding() -> None:
    lines = ["x"] * (SUGGESTION_MAX_LINES + 1)
    long_fix = {**VALID_FINDING, "suggestion": {"before": lines, "after": ["y"]}}

    parsed = parse_review(review_json(long_fix, VALID_FINDING))

    assert len(parsed.review.findings) == 1
    assert parsed.dropped_findings == 1


def test_findings_within_limit_keep_model_order() -> None:
    low = {**VALID_FINDING, "severity": "low", "message": "first"}
    critical = {**VALID_FINDING, "severity": "critical", "message": "second"}

    parsed = parse_review(review_json(low, critical))

    assert [finding.message for finding in parsed.review.findings] == ["first", "second"]
    assert parsed.trimmed_findings == 0


def test_findings_over_limit_keep_the_most_severe() -> None:
    lows = [{**VALID_FINDING, "severity": "low", "message": f"low {i}"} for i in range(45)]
    criticals = [
        {**VALID_FINDING, "severity": "critical", "message": f"critical {i}"} for i in range(10)
    ]

    parsed = parse_review(review_json(*lows, *criticals))

    kept = parsed.review.findings
    assert len(kept) == FINDINGS_MAX
    assert parsed.trimmed_findings == 5
    assert [finding.message for finding in kept[:10]] == [f"critical {i}" for i in range(10)]
    assert kept[10].message == "low 0"
