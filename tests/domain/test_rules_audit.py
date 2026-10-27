"""The rules audit line in the Check Run summary ([R11])."""

from domain.diff import (
    DiffHunk,
    DiffLine,
    DiffLineKind,
    FileDiff,
    FileStatus,
)
from domain.models import Category, DiffSide, Finding, FindingPosition, Severity
from domain.publishing import (
    CommentableLines,
    RulesAudit,
    build_plan,
    rules_audit_line,
)


def finding() -> Finding:
    return Finding(
        position=FindingPosition(path="app/x.py", line=2, side=DiffSide.NEW),
        severity=Severity.HIGH,
        category=Category.SECURITY,
        message="Bind the SQL parameter.",
        suggestion=None,
    )


def lines() -> CommentableLines:
    hunk = DiffHunk(
        old_start=1,
        old_count=1,
        new_start=1,
        new_count=2,
        section=None,
        lines=(
            DiffLine(DiffLineKind.CONTEXT, 1, 1, "base"),
            DiffLine(DiffLineKind.ADDED, None, 2, "new"),
        ),
    )
    diff = FileDiff("app/x.py", "app/x.py", FileStatus.MODIFIED, False, (hunk,))
    return CommentableLines.from_files((diff,))


def test_the_line_counts_applied_and_dropped_rules() -> None:
    audit = RulesAudit(
        ref="f6e5" * 8,
        status="applied",
        rules=6,
        dropped_suspicious=1,
        dropped_over_limit=2,
    )
    assert rules_audit_line(audit) == (
        f"Rules: .review/rules.md @ {'f6e5' * 8} — 6 rules, "
        "1 dropped as suspicious, 2 dropped over the limit."
    )


def test_the_line_for_a_clean_file_has_no_dropped_tail() -> None:
    audit = RulesAudit(ref="a" * 40, status="applied", rules=3)
    assert rules_audit_line(audit) == f"Rules: .review/rules.md @ {'a' * 40} — 3 rules."


def test_rejected_files_say_why_they_were_ignored() -> None:
    assert rules_audit_line(RulesAudit(ref="b" * 40, status="rejected:encoding")) == (
        f"Rules: .review/rules.md @ {'b' * 40} ignored — not valid UTF-8."
    )
    assert rules_audit_line(RulesAudit(ref="c" * 40, status="rejected:too_large")) == (
        f"Rules: .review/rules.md @ {'c' * 40} ignored — too large (over 64 KB)."
    )


def test_build_plan_carries_the_line_only_with_an_audit() -> None:
    with_audit = build_plan(
        (finding(),),
        model_summary="One issue.",
        lines=lines(),
        rules_audit=RulesAudit(ref="d" * 40, status="applied", rules=2),
    )
    without = build_plan((finding(),), model_summary="One issue.", lines=lines())

    assert f"Rules: .review/rules.md @ {'d' * 40} — 2 rules." in with_audit.summary
    assert with_audit.rules_audit is not None
    assert with_audit.rules_audit.rules == 2
    assert "Rules:" not in without.summary
    assert without.rules_audit is None
