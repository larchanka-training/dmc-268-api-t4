"""Step metrics: pr_review_steps.metrics.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05 00:00:00.000000

Hand-written, like 0001-0003 (.agents/rules/backend.md § Migrations). The
column carries the counters of CONTEXT_AND_VERIFICATION_SPEC §1.6 [C6] for the
BUILD_CONTEXT step: counts only, keys allowlisted in domain.jobs
(STEP_METRIC_KEYS) and validated by `validate_step_metrics` on the write path;
never rule text, source or prompt content (AGENTS.md hard rule 3).

Revision 0001 already creates ``metrics`` as NOT NULL JSONB with an empty-object
server default, so this revision makes the column's existence explicit and
idempotent: ``ADD COLUMN IF NOT EXISTS`` adds a nullable ``metrics`` jsonb only
wherever 0001 did not create it, and never conflicts with the stricter
NOT NULL definition 0001 owns. Downgrade is a no-op for the same reason: 0001
owns the column, so dropping it here would break a downgrade to 0002.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE pr_review_steps ADD COLUMN IF NOT EXISTS metrics JSONB NULL")


def downgrade() -> None:
    # 0001 owns the column, so this revision must not drop it.
    pass
