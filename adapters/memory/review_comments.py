"""In-memory published findings: the twin of `adapters.db.review_comments`.

Rejects what the table rejects - an unknown job, or a fingerprint the job already
has (`uq_pr_review_comments_job_fingerprint`) - and then stores nothing, as the
single SQL transaction would.
"""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from adapters.clock import SystemClock
from domain.findings import NewPublishedFinding, PublishedFinding, reading_order
from domain.ports import Clock, JobHistory

_PullRequestKey = tuple[str, int, int, int]


@dataclass(frozen=True, slots=True)
class _Row:
    pull_request: _PullRequestKey
    finding: PublishedFinding


class MemoryReviewCommentStore:
    """Implements `domain.ports.ReviewCommentStore` over a list of rows."""

    def __init__(self, jobs: JobHistory, *, clock: Clock | None = None) -> None:
        self._jobs = jobs
        self._clock = clock or SystemClock()
        self._rows: list[_Row] = []
        self._lock = asyncio.Lock()

    async def record(self, job_id: str, findings: Sequence[NewPublishedFinding]) -> None:
        job = await self._jobs.get(job_id)
        if job is None:
            raise LookupError("no such review job")
        payload = job.payload
        key = (payload.provider, payload.installation_id, payload.repo_id, payload.pr_number)
        async with self._lock:
            taken = {row.finding.fingerprint for row in self._rows if row.finding.job_id == job_id}
            fresh = [item.fingerprint for item in findings]
            if len(set(fresh)) != len(fresh) or taken.intersection(fresh):
                raise ValueError("fingerprint already recorded for this job")
            posted_at = self._clock.now()
            self._rows.extend(
                _Row(key, _published(job_id, item, posted_at=posted_at)) for item in findings
            )

    async def posted_fingerprints(
        self, *, provider: str, installation_id: int, repo_id: int, pr_number: int
    ) -> frozenset[str]:
        key = (provider, installation_id, repo_id, pr_number)
        async with self._lock:
            return frozenset(
                row.finding.fingerprint for row in self._rows if row.pull_request == key
            )

    async def for_job(self, job_id: str) -> tuple[PublishedFinding, ...]:
        async with self._lock:
            found = [row.finding for row in self._rows if row.finding.job_id == job_id]
        return tuple(sorted(found, key=reading_order))


def _published(job_id: str, item: NewPublishedFinding, *, posted_at: datetime) -> PublishedFinding:
    finding = item.finding
    return PublishedFinding(
        id=str(uuid.uuid4()),
        job_id=job_id,
        fingerprint=item.fingerprint,
        path=finding.position.path,
        line=finding.position.line,
        start_line=item.start_line,
        side=finding.position.side,
        severity=finding.severity,
        category=finding.category,
        message=item.body,
        suggestion=finding.suggestion,
        external_comment_id=item.external_comment_id,
        external_review_id=item.external_review_id,
        posted_at=posted_at,
    )
