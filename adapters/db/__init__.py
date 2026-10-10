"""Database adapter package.

Importing the model modules here is load-bearing: Alembic autogenerate compares
Base.metadata against the live database, so a model module that was never
imported looks like a table that should be dropped
(docs/db_models_and_migrations.md §4).
"""

from adapters.db import models  # noqa: F401
