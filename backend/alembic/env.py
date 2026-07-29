"""Alembic migration environment for groundscribe.

Wired to the project's declarative ``Base.metadata`` (so autogenerate sees every
model) and to the shared engine factory (so SQLite migrations get the same
transaction-control / foreign-key behaviour as the app). ``render_as_batch`` is
enabled for SQLite so ALTER-heavy migrations in later phases work there too.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

# Importing the ORM modules registers their tables on ``Base.metadata``; all
# three are needed for the full schema to be visible.
import groundscribe.domain.models
import groundscribe.jobs.models
import groundscribe.provenance.models  # noqa: F401
from groundscribe.db import DEFAULT_URL, Base, create_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    return config.get_main_option("sqlalchemy.url") or DEFAULT_URL


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    backend = url or (connection.engine.url.get_backend_name() if connection else "")
    is_sqlite = backend.startswith("sqlite")
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=is_sqlite,
        literal_binds=url is not None,
        dialect_opts={"paramstyle": "named"} if url is not None else {},
    )


def run_migrations_offline() -> None:
    """Run migrations without a live DBAPI connection (emit SQL)."""
    _configure(url=_resolve_url())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection from the shared engine factory."""
    connectable = create_engine(_resolve_url())
    with connectable.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
