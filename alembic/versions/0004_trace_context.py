"""pr_review_jobs.trace_context: the enqueuing request's trace, for the review to continue.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30 00:00:00.000000

Hand-written, like 0001 (.agents/rules/backend.md § Migrations). The webhook stores the
active request's sentry-trace and baggage headers with the job; the worker continues them
as the review.job span, so the webhook and the review are one trace
(docs/observability.md § Review pipeline; docs/db_models_and_migrations.md). Nullable:
NULL with Sentry off, and for every job enqueued before this column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pr_review_jobs", sa.Column("trace_context", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("pr_review_jobs", "trace_context")
