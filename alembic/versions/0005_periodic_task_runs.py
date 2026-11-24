"""periodic_task_runs: each periodic task's last success, for "Periodic task stale".

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30 00:00:00.000000

Hand-written, like 0001 (.agents/rules/backend.md § Migrations). The leader worker
upserts a row after each successful tick of a periodic task (today only the lease
reaper); the API's DB sampler exports it as periodic_task_last_success_timestamp and
periodic_task_interval_seconds (docs/observability.md § Application, "DB sampler";
docs/db_models_and_migrations.md). Kept in Postgres so the timestamp survives restarts:
a timestamp held in process memory would start at 0 after every deploy.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "periodic_task_runs",
        sa.Column("task", sa.String(64), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_seconds", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("periodic_task_runs")
