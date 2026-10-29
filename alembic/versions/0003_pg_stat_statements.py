"""pg_stat_statements, for the Postgres exporter's top statements.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26 00:00:00.000000

Hand-written, like 0001 (.agents/rules/backend.md § Migrations). The extension's
view only returns rows when the server preloads the library, which
deploy/compose.yml does with ``shared_preload_libraries=pg_stat_statements``; the
CREATE succeeds either way, so the migration does not depend on the restart order
(docs/observability.md § System; docs/db_models_and_migrations.md). Creating it
needs the superuser the postgres image makes from POSTGRES_USER.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
