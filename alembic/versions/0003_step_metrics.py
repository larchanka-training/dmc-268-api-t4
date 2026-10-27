"""Step metrics: pr_review_steps.metrics.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27 00:00:00.000000

Hand-written, like 0001 and 0002 (.agents/rules/backend.md § Migrations). The
column carries the counters of CONTEXT_AND_VERIFICATION_SPEC §1.6 [C6] for the
BUILD_CONTEXT step: counts only, keys allowlisted in domain.jobs
(STEP_METRIC_KEYS); never rule text, source or prompt content (AGENTS.md
hard rule 3). Revision 0001 already creates ``metrics`` as NOT NULL JSONB with
an empty-object server default. This revision intentionally does not alter the
column; both upgrade and downgrade are no-ops to avoid a duplicate add/drop.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0001 already creates metrics as NOT NULL JSONB with a '{}' server default.
    # Keep this revision as a no-op to avoid adding the column twice.
    pass


def downgrade() -> None:
    # 0001 owns the column, so this revision must not drop it.
    pass
