"""Published findings: pr_review_comments.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 00:00:00.000000

Hand-written, like 0001 (.agents/rules/backend.md § Migrations). Deviations
from docs/db_models_and_migrations.md §1.1 are commented on the ORM model in
adapters/db/models.py; severity and category land here as decided in
docs/PIPELINE_SPEC.md D4.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pr_review_comments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("side", sa.String(length=3), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("suggestion", postgresql.JSONB(), nullable=True),
        sa.Column("external_comment_id", sa.BigInteger(), nullable=True),
        sa.Column("external_review_id", sa.BigInteger(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["job_id"], ["pr_review_jobs.id"], ondelete="CASCADE"),
        # Leading column serves the foreign key too: PostgreSQL does not index
        # foreign keys, and cascade deletes would otherwise scan the table.
        sa.UniqueConstraint("job_id", "fingerprint", name="uq_pr_review_comments_job_fingerprint"),
        sa.CheckConstraint("side IN ('old','new')", name="ck_pr_review_comments_side"),
        sa.CheckConstraint(
            "severity IN ('critical','high','medium','low')",
            name="ck_pr_review_comments_severity",
        ),
        sa.CheckConstraint(
            "category IN ('security','correctness','concurrency','performance','maintainability')",
            name="ck_pr_review_comments_category",
        ),
        sa.CheckConstraint(
            "line_number >= 1 AND (start_line IS NULL OR "
            "(start_line >= 1 AND start_line <= line_number))",
            name="ck_pr_review_comments_lines",
        ),
    )


def downgrade() -> None:
    op.drop_table("pr_review_comments")
