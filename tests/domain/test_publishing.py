"""What a review posts, decided without a forge."""

import pytest

from domain.models import (
    Category,
    DiffSide,
    Finding,
    FindingPosition,
    FindingSuggestion,
    Severity,
)
from domain.publishing import (
    SUMMARY_FILES_SHOWN,
    SUMMARY_MARKER,
    CheckConclusion,
    CommentableLines,
    PublishPolicy,
    ReviewEvent,
    Verdict,
    at_least,
    build_plan,
    fingerprint,
    fingerprint_in,
)

PATCH = """@@ -10,4 +10,5 @@ def list_users(conn, sort):
     query = "SELECT id, name FROM users"
-    rows = conn.execute(query).fetchall()
+    query += f" ORDER BY {sort}"
+    rows = conn.execute(query).fetchall()
     return rows
\\ No newline at end of file"""


def lines() -> CommentableLines:
    return CommentableLines.from_patches({"app/users.py": PATCH})


def finding(
    line: int = 12,
    *,
    side: DiffSide = DiffSide.NEW,
    severity: Severity = Severity.HIGH,
    message: str = "The sort parameter is interpolated into SQL.",
    path: str = "app/users.py",
    suggestion: FindingSuggestion | None = None,
) -> Finding:
    return Finding(
        position=FindingPosition(path=path, line=line, side=side),
        severity=severity,
        category=Category.SECURITY,
        message=message,
        suggestion=suggestion,
    )


# --- The diff ---------------------------------------------------------------


def test_patch_lines_are_numbered_per_side() -> None:
    parsed = lines()

    assert parsed.text_at("app/users.py", DiffSide.OLD, 11) == (
        "    rows = conn.execute(query).fetchall()"
    )
    assert parsed.text_at("app/users.py", DiffSide.NEW, 11) == '    query += f" ORDER BY {sort}"'
    assert parsed.text_at("app/users.py", DiffSide.NEW, 12) == (
        "    rows = conn.execute(query).fetchall()"
    )
    # Context lines are commentable on either side, at their own numbers.
    assert parsed.text_at("app/users.py", DiffSide.OLD, 12) == "    return rows"
    assert parsed.text_at("app/users.py", DiffSide.NEW, 13) == "    return rows"
    # Nothing past the hunk, and the "no newline" marker is not a line.
    assert parsed.text_at("app/users.py", DiffSide.NEW, 14) is None
    assert parsed.paths == ("app/users.py",)


# --- Fingerprints -------------------------------------------------------------


def test_fingerprint_is_stable_across_releases() -> None:
    # Golden value. If this changes, every finding already posted is posted again.
    assert fingerprint(finding(), "    rows = conn.execute(query).fetchall()") == (
        "eef7989cfc8ce9b2e284ee00637d8f14d94d089fff465933a0a1a1e2672fcbd8"
    )


def test_fingerprint_ignores_the_line_number_and_whitespace() -> None:
    code = "rows = conn.execute(query).fetchall()"

    moved = fingerprint(finding(line=40), code)

    assert moved == fingerprint(finding(line=12), code)
    assert fingerprint(finding(message="The  sort parameter\nis interpolated into SQL."), code) == (
        moved
    )
    assert fingerprint(finding(), f"   {code}  ") == moved
    assert fingerprint(finding(), "other code") != moved


def test_a_comment_body_carries_its_fingerprint() -> None:
    plan = build_plan([finding()], model_summary="", lines=lines())

    comment = plan.comments[0]

    assert fingerprint_in(comment.body) == comment.fingerprint
    assert fingerprint_in("no marker here") is None


# --- Filters ------------------------------------------------------------------


def test_a_position_outside_the_diff_is_dropped() -> None:
    plan = build_plan(
        [finding(line=99), finding(path="app/other.py"), finding()],
        model_summary="",
        lines=lines(),
    )

    assert [comment.finding.position.line for comment in plan.comments] == [12]
    assert plan.stats.outside_diff == 2


def test_findings_below_min_severity_are_dropped() -> None:
    plan = build_plan(
        [finding(severity=Severity.LOW), finding(severity=Severity.MEDIUM, message="other")],
        model_summary="",
        lines=lines(),
        policy=PublishPolicy(min_severity=Severity.MEDIUM),
    )

    assert [comment.finding.severity for comment in plan.comments] == [Severity.MEDIUM]
    assert plan.stats.below_threshold == 1


def test_already_posted_and_repeated_findings_are_not_posted_again() -> None:
    first = build_plan([finding()], model_summary="", lines=lines())
    posted = frozenset({first.comments[0].fingerprint})

    again = build_plan(
        [finding(), finding(), finding(line=11, message="new problem")],
        model_summary="",
        lines=lines(),
        posted_fingerprints=posted,
    )

    assert [comment.finding.message for comment in again.comments] == ["new problem"]
    assert again.stats.already_posted == 2


def test_the_comment_cap_keeps_the_most_severe() -> None:
    plan = build_plan(
        [
            finding(line=11, severity=Severity.MEDIUM, message="medium"),
            finding(line=12, severity=Severity.CRITICAL, message="critical"),
            finding(line=13, severity=Severity.HIGH, message="high"),
        ],
        model_summary="",
        lines=lines(),
        policy=PublishPolicy(max_comments=2),
    )

    assert [comment.finding.message for comment in plan.comments] == ["critical", "high"]
    assert plan.stats.over_limit == 1


def test_severity_order() -> None:
    assert at_least(Severity.CRITICAL, Severity.HIGH)
    assert at_least(Severity.HIGH, Severity.HIGH)
    assert not at_least(Severity.LOW, Severity.MEDIUM)


def test_a_negative_comment_cap_is_refused() -> None:
    with pytest.raises(ValueError):
        PublishPolicy(max_comments=-1)


# --- Verdict --------------------------------------------------------------------


def test_no_findings_is_a_clean_success_with_no_review() -> None:
    plan = build_plan([], model_summary="Looks fine.", lines=lines())

    assert plan.verdict is Verdict.CLEAN
    assert plan.conclusion is CheckConclusion.SUCCESS
    assert plan.review_event is ReviewEvent.COMMENT
    assert not plan.needs_review


def test_findings_are_neutral_by_default_so_the_bot_never_blocks() -> None:
    plan = build_plan([finding(severity=Severity.CRITICAL)], model_summary="", lines=lines())

    assert plan.verdict is Verdict.COMMENTS
    assert plan.conclusion is CheckConclusion.NEUTRAL
    assert plan.review_event is ReviewEvent.COMMENT
    assert plan.needs_review


def test_blocking_is_opt_in() -> None:
    plan = build_plan(
        [finding(severity=Severity.HIGH)],
        model_summary="",
        lines=lines(),
        policy=PublishPolicy(request_changes_at=Severity.HIGH),
    )

    assert plan.verdict is Verdict.CHANGES_REQUESTED
    assert plan.conclusion is CheckConclusion.FAILURE
    assert plan.review_event is ReviewEvent.REQUEST_CHANGES


def test_a_re_review_with_nothing_new_keeps_the_verdict_but_posts_no_review() -> None:
    first = build_plan([finding()], model_summary="", lines=lines())

    again = build_plan(
        [finding()],
        model_summary="",
        lines=lines(),
        posted_fingerprints=frozenset({first.comments[0].fingerprint}),
    )

    assert again.verdict is Verdict.COMMENTS
    assert again.comments == ()
    assert not again.needs_review


# --- Rendering ------------------------------------------------------------------


def test_a_matching_suggestion_becomes_a_github_suggestion_over_its_lines() -> None:
    fix = FindingSuggestion(
        before=('    query += f" ORDER BY {sort}"', "    rows = conn.execute(query).fetchall()"),
        after=(
            '    query += f" ORDER BY {COLUMNS[sort]}"',
            "    rows = conn.execute(query).fetchall()",
        ),
    )

    plan = build_plan([finding(suggestion=fix)], model_summary="", lines=lines())

    comment = plan.comments[0]
    assert comment.start_line == 11
    assert "```suggestion\n" in comment.body
    assert "COLUMNS[sort]" in comment.body


def test_a_suggestion_that_does_not_match_the_diff_is_shown_as_a_plain_diff() -> None:
    fix = FindingSuggestion(before=("something else",), after=("better",))

    plan = build_plan([finding(suggestion=fix)], model_summary="", lines=lines())

    comment = plan.comments[0]
    assert comment.start_line is None
    assert "```suggestion" not in comment.body
    assert "-something else\n+better" in comment.body


def test_an_old_side_suggestion_is_never_a_github_suggestion() -> None:
    fix = FindingSuggestion(before=("    rows = conn.execute(query).fetchall()",), after=("x",))

    plan = build_plan(
        [finding(line=11, side=DiffSide.OLD, suggestion=fix)], model_summary="", lines=lines()
    )

    assert "```suggestion" not in plan.comments[0].body


def test_the_summary_has_verdict_risks_and_files() -> None:
    plan = build_plan(
        [finding(severity=Severity.CRITICAL), finding(line=11, severity=Severity.LOW)],
        model_summary="Adds sorting to the users list.",
        lines=lines(),
        policy=PublishPolicy(min_severity=Severity.LOW),
    )

    summary = plan.summary
    assert summary.startswith(SUMMARY_MARKER)
    assert "Adds sorting to the users list." in summary
    assert "🔴 Critical: 1" in summary
    assert "🔵 Low: 1" in summary
    assert "`app/users.py:12`" in summary
    assert "### Files reviewed (1)" in summary


def test_the_summary_lists_a_bounded_number_of_files() -> None:
    many = CommentableLines.from_patches(
        {f"src/file_{index:03}.py": "@@ -1 +1 @@\n-a\n+b" for index in range(40)}
    )

    plan = build_plan([], model_summary="", lines=many)

    assert plan.summary.count("- `src/file_") == SUMMARY_FILES_SHOWN
    assert f"…and {40 - SUMMARY_FILES_SHOWN} more" in plan.summary


def test_redaction_applies_to_everything_that_leaves() -> None:
    secret = "ghp_" + "a" * 36
    plan = build_plan(
        [finding(message=f"Token {secret} is committed.")],
        model_summary=f"Found {secret}.",
        lines=lines(),
        redact=lambda text: text.replace(secret, "[REDACTED]"),
    )

    assert secret not in plan.summary
    assert secret not in plan.comments[0].body
    assert "[REDACTED]" in plan.comments[0].body
