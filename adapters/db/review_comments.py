"""Published findings in PostgreSQL (`pr_review_comments`).

Raw ``text()`` SQL like the job store. `record` is one transaction: the foreign
key rejects an unknown job and `uq_pr_review_comments_job_fingerprint` a repeated
fingerprint, and either rolls the whole batch back. `posted_at` is the database's
``now()``. `posted_fingerprints` walks the pull request's jobs through
`ix_pr_review_jobs_pr_history` and their rows through the unique constraint's
leading `job_id`.
"""

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine

from domain.findings import NewPublishedFinding, PublishedFinding, reading_order
from domain.models import Category, DiffSide, FindingSuggestion, Severity

_INSERT = text(
    "INSERT INTO pr_review_comments"
    " (id, job_id, fingerprint, file_path, line_number, start_line, side, severity,"
    " category, content, suggestion, external_comment_id, external_review_id, posted_at)"
    " VALUES (:id, :job_id, :fingerprint, :file_path, :line_number, :start_line, :side,"
    " :severity, :category, :content, CAST(:suggestion AS jsonb), :external_comment_id,"
    " :external_review_id, now())"
)

_POSTED_FINGERPRINTS = text(
    "SELECT DISTINCT c.fingerprint FROM pr_review_jobs j"
    " JOIN pr_review_comments c ON c.job_id = j.id"
    " WHERE j.provider = :provider AND j.installation_id = :installation_id"
    " AND j.repo_id = :repo_id AND j.pr_number = :pr_number"
)

_FOR_JOB = text(
    "SELECT id, job_id, fingerprint, file_path, line_number, start_line, side, severity,"
    " category, content, suggestion, external_comment_id, external_review_id, posted_at"
    " FROM pr_review_comments WHERE job_id = :job_id"
)


class PostgresReviewCommentStore:
    """Implements `domain.ports.ReviewCommentStore` over `pr_review_comments`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, job_id: str, findings: Sequence[NewPublishedFinding]) -> None:
        row_id = _as_uuid(job_id)
        if row_id is None:
            raise LookupError("no such review job")
        if not findings:
            return
        async with self._engine.begin() as conn:
            await conn.execute(_INSERT, [_insert_params(row_id, item) for item in findings])

    async def posted_fingerprints(
        self, *, provider: str, installation_id: int, repo_id: int, pr_number: int
    ) -> frozenset[str]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                _POSTED_FINGERPRINTS,
                {
                    "provider": provider,
                    "installation_id": installation_id,
                    "repo_id": repo_id,
                    "pr_number": pr_number,
                },
            )
            return frozenset(str(row.fingerprint) for row in result)

    async def for_job(self, job_id: str) -> tuple[PublishedFinding, ...]:
        row_id = _as_uuid(job_id)
        if row_id is None:
            return ()
        async with self._engine.connect() as conn:
            result = await conn.execute(_FOR_JOB, {"job_id": row_id})
            found = [_finding_from_row(row) for row in result]
        return tuple(sorted(found, key=reading_order))


def _insert_params(job_id: uuid.UUID, item: NewPublishedFinding) -> dict[str, Any]:
    finding = item.finding
    suggestion = finding.suggestion
    return {
        "id": uuid.uuid4(),
        "job_id": job_id,
        "fingerprint": item.fingerprint,
        "file_path": finding.position.path,
        "line_number": finding.position.line,
        "start_line": item.start_line,
        "side": str(finding.position.side),
        "severity": str(finding.severity),
        "category": str(finding.category),
        "content": item.body,
        "suggestion": None
        if suggestion is None
        else json.dumps({"before": list(suggestion.before), "after": list(suggestion.after)}),
        "external_comment_id": item.external_comment_id,
        "external_review_id": item.external_review_id,
    }


def _finding_from_row(row: Row[Any]) -> PublishedFinding:
    return PublishedFinding(
        id=row.id.hex,
        job_id=row.job_id.hex,
        fingerprint=row.fingerprint,
        path=row.file_path,
        line=row.line_number,
        start_line=row.start_line,
        side=DiffSide(row.side),
        severity=Severity(row.severity),
        category=Category(row.category),
        message=row.content,
        suggestion=_suggestion_from_json(row.suggestion),
        external_comment_id=row.external_comment_id,
        external_review_id=row.external_review_id,
        posted_at=row.posted_at,
    )


def _suggestion_from_json(raw: Any) -> FindingSuggestion | None:
    if raw is None:
        return None
    # asyncpg hands raw-SELECT jsonb back as text; accept either shape.
    value = json.loads(raw) if isinstance(raw, str) else raw
    return FindingSuggestion(
        before=tuple(str(line) for line in value["before"]),
        after=tuple(str(line) for line in value["after"]),
    )


def _as_uuid(job_id: str) -> uuid.UUID | None:
    """Job ids are uuid4 hex strings; anything else is simply no job."""
    try:
        return uuid.UUID(job_id)
    except ValueError:
        return None
