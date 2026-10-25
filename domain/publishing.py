"""What a review posts on a pull request, decided before anything is sent. Pure: no I/O.

The worker's review stage (`worker/process.py`, `ReviewStage`) hands the LLM findings in;
out comes a `PublishPlan` that a forge adapter (`domain.ports.ReviewPublisher`) turns
into API calls. Commentable positions come from the parsed diff
(`CommentableLines.from_files`). Every rule below is tested without a forge.

Order of the filters follows PIPELINE_SPEC.md §6.2, steps 3-5, plus the comment cap from
configuration.md §3:

1. a finding whose (path, line, side) is not in the diff is dropped — a hallucinated
   position cannot become an inline comment, and GitHub answers 422 for it;
2. findings below `min_severity` are dropped;
3. findings whose fingerprint was already posted on this pull request are dropped;
4. at most `max_comments` remain, most severe first.

The worker passes an empty `posted_fingerprints`, so a re-review posts every finding
again, and the `PublishPolicy` defaults apply to every repository. All model text passes
through `redact`, which by default returns it unchanged.
"""

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from domain.diff import FileDiff
from domain.models import RISK_ORDER, DiffSide, Finding, FindingPosition, RiskLevel, Severity

# Most severe first: Severity is declared in that order in domain/models.py.
SEVERITY_ORDER: tuple[Severity, ...] = tuple(Severity)
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITY_ORDER)}

# A hidden marker in every comment body. It lets the adapter find the ids of the
# comments it just created, and lets a lost database be rebuilt from the forge.
FINGERPRINT_MARKER = "<!-- dmc268:fp={fingerprint} -->"
_FINGERPRINT_MARKER_RE = re.compile(r"<!-- dmc268:fp=(?P<fingerprint>[0-9a-f]{64}) -->")
SUMMARY_MARKER = "<!-- dmc268:summary -->"
NO_ISSUES_LINE = "No issues found in this review."

SUMMARY_FILES_SHOWN = 30
SUMMARY_RISKS_SHOWN = 10
# GitHub caps a check run summary at 65535 characters; keep well clear of it.
SUMMARY_MAX_LENGTH = 60_000

_HUNK_HEADER = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")

Redactor = Callable[[str], str]


def _no_redaction(text: str) -> str:
    return text


class ReviewEvent(StrEnum):
    """The kind of pull request review posted, in GitHub's names."""

    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class CheckConclusion(StrEnum):
    SUCCESS = "success"
    NEUTRAL = "neutral"
    FAILURE = "failure"


class Verdict(StrEnum):
    CLEAN = "clean"
    COMMENTS = "comments"
    CHANGES_REQUESTED = "changes_requested"


@dataclass(frozen=True, slots=True)
class PublishPolicy:
    """Per-repository output settings (configuration.md §3, "Output").

    By default the bot never blocks a merge: `request_changes_at` is `None`, so the check
    conclusion is at most `neutral`. A severity there makes findings at or above it
    request changes.
    """

    min_severity: Severity = Severity.MEDIUM
    max_comments: int = 25
    request_changes_at: Severity | None = None

    def __post_init__(self) -> None:
        if self.max_comments < 0:
            raise ValueError(f"max_comments must be >= 0, got {self.max_comments}")


DEFAULT_POLICY = PublishPolicy()


def at_least(severity: Severity, threshold: Severity) -> bool:
    """True when `severity` is as loud as `threshold` or louder."""
    return _SEVERITY_RANK[severity] <= _SEVERITY_RANK[threshold]


@dataclass(frozen=True, slots=True)
class CommentableLines:
    """The lines of a diff a comment may be anchored to, with their text.

    The worker builds it from the parsed diff (`from_files`); `from_patches` reads the
    per-file patches GitHub returns (`files[].patch`).
    """

    lines: Mapping[str, Mapping[tuple[DiffSide, int], str]] = field(default_factory=dict)

    @classmethod
    def from_patches(cls, patches: Mapping[str, str]) -> "CommentableLines":
        return cls({path: _parse_patch(patch) for path, patch in patches.items()})

    @classmethod
    def from_files(cls, files: Iterable[FileDiff]) -> "CommentableLines":
        """From the parsed diff (domain/diff.py): every line of every hunk."""
        found: dict[str, dict[tuple[DiffSide, int], str]] = {}
        for diff in files:
            path = diff.new_path or diff.old_path
            lines = found.setdefault(path, {})
            for hunk in diff.hunks:
                for line in hunk.lines:
                    if line.new_lineno is not None:
                        lines[(DiffSide.NEW, line.new_lineno)] = line.content
                    if line.old_lineno is not None:
                        lines[(DiffSide.OLD, line.old_lineno)] = line.content
        return cls(found)

    def contains(self, position: FindingPosition) -> bool:
        return (position.side, position.line) in self.lines.get(position.path, {})

    def text_at(self, path: str, side: DiffSide, line: int) -> str | None:
        return self.lines.get(path, {}).get((side, line))

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.lines))


def _parse_patch(patch: str) -> dict[tuple[DiffSide, int], str]:
    found: dict[tuple[DiffSide, int], str] = {}
    old_line = new_line = 0
    in_hunk = False
    for raw in patch.splitlines():
        header = _HUNK_HEADER.match(raw)
        if header is not None:
            old_line, new_line = int(header["old"]), int(header["new"])
            in_hunk = True
            continue
        if not in_hunk or raw.startswith("\\"):
            # Before the first hunk, or "\ No newline at end of file".
            continue
        marker, text = raw[:1], raw[1:]
        if marker == "+":
            found[(DiffSide.NEW, new_line)] = text
            new_line += 1
        elif marker == "-":
            found[(DiffSide.OLD, old_line)] = text
            old_line += 1
        else:
            # Context: GitHub accepts a comment on either side of it.
            found[(DiffSide.NEW, new_line)] = text
            found[(DiffSide.OLD, old_line)] = text
            old_line += 1
            new_line += 1
    return found


def fingerprint(finding: Finding, anchored_code: str) -> str:
    """sha256(path + normalised message + anchored code), WORKFLOW_DESIGN.md §5.

    The line number is deliberately left out: an unrelated insertion above the finding
    shifts it, and a line-keyed fingerprint would re-post the whole file. This value
    is stored on every posted comment, so changing the formula re-posts every finding
    ever made — the golden test in tests/domain/test_publishing.py guards it.
    """
    normalised_message = " ".join(finding.message.lower().split())
    normalised_code = " ".join(anchored_code.split())
    payload = "\0".join((finding.position.path, normalised_message, normalised_code))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_in(body: str) -> str | None:
    """The fingerprint marker of a comment body, if it carries one."""
    match = _FINGERPRINT_MARKER_RE.search(body)
    return None if match is None else match["fingerprint"]


@dataclass(frozen=True, slots=True)
class PlannedComment:
    """One inline comment, ready to post. `start_line` is set for a multi-line one."""

    finding: Finding
    fingerprint: str
    body: str
    start_line: int | None = None


@dataclass(frozen=True, slots=True)
class PublishStats:
    """Counts for the POSTPROCESS step's metrics. Shape only, never content."""

    received: int
    outside_diff: int
    below_threshold: int
    already_posted: int
    over_limit: int
    by_severity: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class PublishPlan:
    comments: tuple[PlannedComment, ...]
    summary: str
    verdict: Verdict
    review_event: ReviewEvent
    conclusion: CheckConclusion
    stats: PublishStats
    risk: RiskLevel = RiskLevel.LOW

    @property
    def needs_review(self) -> bool:
        """Post a pull request review only when it says something new.

        The summary lives in the Check Run, updated per head SHA, so a re-review with
        no new comments posts no review at all instead of one more "nothing new".
        """
        return bool(self.comments) or self.review_event is ReviewEvent.REQUEST_CHANGES


def risk_floor(findings: Iterable[Finding]) -> RiskLevel:
    """The least risk the findings imply: a critical one makes it High, a high one Medium."""
    severities = {finding.severity for finding in findings}
    if Severity.CRITICAL in severities:
        return RiskLevel.HIGH
    if Severity.HIGH in severities:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def assess_risk(
    model_risk: RiskLevel | None, findings: Iterable[Finding], *, dismissed: int = 0
) -> RiskLevel:
    """The model's risk, never below what the findings imply; the floor alone without one.

    The model sees the whole change and may rate it higher than its findings do, but a
    critical finding must never sit under a "Low" headline (issue #55).

    The model rates the risk before the verifier runs, so it may have counted findings the
    verifier then dismissed. When the verifier dismissed any (`dismissed` > 0), the model's
    risk is capped at max(floor, Medium): a dismissed finding can never leave "High" behind
    on its own, while the change area alone can still justify Medium.
    """
    floor = risk_floor(findings)
    if model_risk is None:
        return floor
    risk = model_risk
    if dismissed > 0:
        cap = max(floor, RiskLevel.MEDIUM, key=RISK_ORDER.index)
        risk = min(risk, cap, key=RISK_ORDER.index)
    return max(risk, floor, key=RISK_ORDER.index)


DISMISSED_NOTE = "The summary and checklist above were written before that check."


def dismissed_line(dismissed: int) -> str:
    """Counts only: the dismissed findings' text is never shown.

    The model writes the summary and checklist before the verifier runs, and a prompt rule
    keeping defects out of them cost eval precision and recall (issue #55 follow-up), so the
    Check Run says plainly which came first.
    """
    if dismissed == 1:
        counted = "1 possible issue was checked by the verifier and dismissed."
    else:
        counted = f"{dismissed} possible issues were checked by the verifier and dismissed."
    return f"{counted} {DISMISSED_NOTE}"


def build_plan(
    findings: Iterable[Finding],
    *,
    model_summary: str,
    lines: CommentableLines,
    policy: PublishPolicy = DEFAULT_POLICY,
    posted_fingerprints: frozenset[str] = frozenset(),
    redact: Redactor = _no_redaction,
    model_risk: RiskLevel | None = None,
    checklist: tuple[str, ...] = (),
    dismissed_by_verifier: int = 0,
) -> PublishPlan:
    received = tuple(findings)
    in_diff = [finding for finding in received if lines.contains(finding.position)]
    loud_enough = [
        finding for finding in in_diff if at_least(finding.severity, policy.min_severity)
    ]

    fresh: list[tuple[Finding, str]] = []
    seen: set[str] = set()
    for finding in loud_enough:
        anchored = lines.text_at(
            finding.position.path, finding.position.side, finding.position.line
        )
        digest = fingerprint(finding, anchored or "")
        # A model repeating itself inside one reply is a duplicate too.
        if digest in posted_fingerprints or digest in seen:
            continue
        seen.add(digest)
        fresh.append((finding, digest))

    fresh.sort(key=lambda pair: (_SEVERITY_RANK[pair[0].severity], pair[0].position.path))
    kept = fresh[: policy.max_comments]
    comments = tuple(_plan_comment(finding, digest, lines, redact) for finding, digest in kept)

    blocking = policy.request_changes_at is not None and any(
        at_least(finding.severity, policy.request_changes_at) for finding in loud_enough
    )
    if blocking:
        verdict, event, conclusion = (
            Verdict.CHANGES_REQUESTED,
            ReviewEvent.REQUEST_CHANGES,
            CheckConclusion.FAILURE,
        )
    elif loud_enough:
        # Findings already posted earlier still stand: the verdict is about the PR, not
        # about what is new in this round.
        verdict, event, conclusion = Verdict.COMMENTS, ReviewEvent.COMMENT, CheckConclusion.NEUTRAL
    else:
        verdict, event, conclusion = Verdict.CLEAN, ReviewEvent.COMMENT, CheckConclusion.SUCCESS

    stats = PublishStats(
        received=len(received),
        outside_diff=len(received) - len(in_diff),
        below_threshold=len(in_diff) - len(loud_enough),
        already_posted=len(loud_enough) - len(fresh),
        over_limit=len(fresh) - len(kept),
        by_severity=dict(Counter(str(finding.severity) for finding in loud_enough)),
    )
    risk = assess_risk(model_risk, loud_enough, dismissed=dismissed_by_verifier)
    summary = render_summary(
        model_summary=model_summary,
        verdict=verdict,
        findings=tuple(loud_enough),
        files=lines.paths,
        stats=stats,
        redact=redact,
        risk=risk,
        model_risk=model_risk,
        checklist=checklist,
        dismissed=dismissed_by_verifier,
    )
    return PublishPlan(
        comments=comments,
        summary=summary,
        verdict=verdict,
        review_event=event,
        conclusion=conclusion,
        stats=stats,
        risk=risk,
    )


_SEVERITY_LABEL = {
    Severity.CRITICAL: "🔴 Critical",
    Severity.HIGH: "🟠 High",
    Severity.MEDIUM: "🟡 Medium",
    Severity.LOW: "🔵 Low",
}

_RISK_LABEL = {
    RiskLevel.HIGH: "High",
    RiskLevel.MEDIUM: "Medium",
    RiskLevel.LOW: "Low",
}

_VERDICT_LABEL = {
    Verdict.CLEAN: "✅ No issues at or above the configured severity",
    Verdict.COMMENTS: "💬 Issues worth a look",
    Verdict.CHANGES_REQUESTED: "⛔ Changes requested",
}


def _plan_comment(
    finding: Finding, digest: str, lines: CommentableLines, redact: Redactor
) -> PlannedComment:
    start_line = _suggestion_start(finding, lines)
    return PlannedComment(
        finding=finding,
        fingerprint=digest,
        body=render_comment(finding, digest, as_suggestion=start_line is not None, redact=redact),
        start_line=start_line if start_line != finding.position.line else None,
    )


def _suggestion_start(finding: Finding, lines: CommentableLines) -> int | None:
    """The first line a ```suggestion block would replace, or None if it cannot be one.

    A GitHub suggestion replaces exactly the commented lines with its content, so it is
    only offered when `before` matches the new-side lines ending at `line`. On the old
    side, or when the model's `before` does not match the diff, applying it would
    corrupt the file; the fix is shown as a plain diff instead.
    """
    suggestion = finding.suggestion
    position = finding.position
    if suggestion is None or not suggestion.before or position.side is not DiffSide.NEW:
        return None
    first = position.line - len(suggestion.before) + 1
    if first < 1:
        return None
    for offset, expected in enumerate(suggestion.before):
        actual = lines.text_at(position.path, DiffSide.NEW, first + offset)
        if actual is None or actual.rstrip() != expected.rstrip():
            return None
    return first


def render_comment(
    finding: Finding,
    digest: str,
    *,
    as_suggestion: bool,
    redact: Redactor = _no_redaction,
) -> str:
    parts = [
        f"**{_SEVERITY_LABEL[finding.severity]}** · `{finding.category}`",
        "",
        redact(finding.message),
    ]
    suggestion = finding.suggestion
    if suggestion is not None:
        parts.append("")
        if as_suggestion:
            parts.extend(["```suggestion", *map(redact, suggestion.after), "```"])
        else:
            parts.extend(
                [
                    "Suggested change:",
                    "```diff",
                    *(f"-{redact(line)}" for line in suggestion.before),
                    *(f"+{redact(line)}" for line in suggestion.after),
                    "```",
                ]
            )
    parts.extend(["", FINGERPRINT_MARKER.format(fingerprint=digest)])
    return "\n".join(parts)


def _risk_line(risk: RiskLevel, model_risk: RiskLevel | None) -> str:
    line = f"**Risk: {_RISK_LABEL[risk]}**"
    if model_risk is None:
        return f"{line} (from the findings' severity)"
    if RISK_ORDER.index(risk) > RISK_ORDER.index(model_risk):
        return f"{line} (the model said {_RISK_LABEL[model_risk]}; the findings set the floor)"
    if RISK_ORDER.index(risk) < RISK_ORDER.index(model_risk):
        return (
            f"{line} (the model said {_RISK_LABEL[model_risk]} before the verifier dismissed "
            "findings; without a published finding to back it, the risk is capped)"
        )
    return line


def render_summary(
    *,
    model_summary: str,
    verdict: Verdict,
    findings: tuple[Finding, ...],
    files: tuple[str, ...],
    stats: PublishStats,
    redact: Redactor = _no_redaction,
    risk: RiskLevel | None = None,
    model_risk: RiskLevel | None = None,
    checklist: tuple[str, ...] = (),
    dismissed: int = 0,
) -> str:
    """The overview: verdict, the model's summary, risk, checklist, risks by severity, files."""
    parts = [SUMMARY_MARKER, f"## {_VERDICT_LABEL[verdict]}", ""]
    if model_summary.strip():
        parts.extend([redact(model_summary.strip()), ""])
    shown_risk = assess_risk(model_risk, findings, dismissed=dismissed) if risk is None else risk
    parts.extend([_risk_line(shown_risk, model_risk), ""])
    if not findings:
        parts.extend([NO_ISSUES_LINE, ""])
    if dismissed > 0:
        parts.extend([dismissed_line(dismissed), ""])
    if checklist:
        parts.extend(["### Checklist for the reviewer", ""])
        parts.extend(f"- {redact(item)}" for item in checklist)
        parts.append("")

    if findings:
        counts = " · ".join(
            f"{_SEVERITY_LABEL[severity]}: {stats.by_severity[str(severity)]}"
            for severity in SEVERITY_ORDER
            if stats.by_severity.get(str(severity))
        )
        parts.extend(["### Risks", "", counts, ""])
        ranked = sorted(
            findings,
            key=lambda finding: (_SEVERITY_RANK[finding.severity], finding.position.path),
        )
        for finding in ranked[:SUMMARY_RISKS_SHOWN]:
            headline = redact(finding.message).splitlines()[0]
            parts.append(
                f"- **{_SEVERITY_LABEL[finding.severity]}** "
                f"`{finding.position.path}:{finding.position.line}` {headline}"
            )
        if len(ranked) > SUMMARY_RISKS_SHOWN:
            parts.append(f"- …and {len(ranked) - SUMMARY_RISKS_SHOWN} more")
        parts.append("")

    parts.extend([f"### Files reviewed ({len(files)})", ""])
    parts.extend(f"- `{path}`" for path in files[:SUMMARY_FILES_SHOWN])
    if len(files) > SUMMARY_FILES_SHOWN:
        parts.append(f"- …and {len(files) - SUMMARY_FILES_SHOWN} more")

    hidden = stats.already_posted + stats.over_limit
    if hidden:
        parts.extend(
            [
                "",
                f"_{stats.already_posted} finding(s) were already posted on this pull request; "
                f"{stats.over_limit} more were not posted inline because of the comment limit._",
            ]
        )
    summary = "\n".join(parts)
    if len(summary) > SUMMARY_MAX_LENGTH:
        summary = summary[: SUMMARY_MAX_LENGTH - 1] + "…"
    return summary
