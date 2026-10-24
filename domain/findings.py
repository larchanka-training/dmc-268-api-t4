"""Published findings: what reached the forge, as `pr_review_comments` keeps it.

A row exists only for a finding the forge accepted, inline or in the review body
(PIPELINE_SPEC.md §7.1). Rows are written in one batch right after the review is
posted, because GitHub posts a review as one request; a crash between the two is
the one window in which a retry posts again (WORKFLOW_DESIGN.md §5 step 3 assumes
one request per comment).
"""

from dataclasses import dataclass
from datetime import datetime

from domain.models import Category, DiffSide, Finding, FindingSuggestion, Severity
from domain.ports import PublishOutcome
from domain.publishing import PublishPlan


@dataclass(frozen=True, slots=True)
class NewPublishedFinding:
    """One finding to record, built from the plan and what the forge returned."""

    fingerprint: str
    finding: Finding
    # The comment as posted, after redaction - never the raw model reply.
    body: str
    start_line: int | None
    # None when the comment went into the review body instead of inline.
    external_comment_id: int | None
    external_review_id: int | None


@dataclass(frozen=True, slots=True)
class PublishedFinding:
    """One stored finding, as the API returns it."""

    id: str
    job_id: str
    fingerprint: str
    path: str
    line: int
    start_line: int | None
    side: DiffSide
    severity: Severity
    category: Category
    message: str
    suggestion: FindingSuggestion | None
    external_comment_id: int | None
    external_review_id: int | None
    posted_at: datetime


def published_findings(
    plan: PublishPlan, outcome: PublishOutcome
) -> tuple[NewPublishedFinding, ...]:
    """The planned comments the forge reported back, in plan order.

    A planned comment missing from the outcome did not reach the forge and gets no
    row, so it stays eligible for the next review."""
    posted = {comment.fingerprint: comment for comment in outcome.comments}
    return tuple(
        NewPublishedFinding(
            fingerprint=planned.fingerprint,
            finding=planned.finding,
            body=planned.body,
            start_line=planned.start_line,
            external_comment_id=posted[planned.fingerprint].external_comment_id,
            external_review_id=outcome.review_id,
        )
        for planned in plan.comments
        if planned.fingerprint in posted
    )


_SEVERITY_RANK = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
}


def reading_order(finding: PublishedFinding) -> tuple[int, str, int]:
    """Most severe first, then by file and line: what to read first (§7.1)."""
    return (_SEVERITY_RANK[finding.severity], finding.path, finding.line)
