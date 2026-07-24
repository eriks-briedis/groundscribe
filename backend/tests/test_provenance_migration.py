"""Provenance-schema migration tests (phase 03).

Spec (plan/03 → Implementation tasks): the provenance entities and their
hierarchy ship with migrations. The round trip is what keeps the SQLite-dev /
PostgreSQL-prod path trivial (plan/00 → Tech stack), so it is asserted rather
than assumed.

The stored guarantees are re-checked *against the migrated schema*, not only
against ``create_all``: a CHECK constraint or a unique index that the models
declare but the migration omits would leave production without the guarantee the
tests appear to prove.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from groundscribe.db import create_engine
from groundscribe.records import EVALUATION_TABLES, EXECUTION_TABLES

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"

EXPECTED_TABLES = EXECUTION_TABLES | EVALUATION_TABLES

#: The minimum chain of rows a stage execution depends on, written as raw SQL so
#: the migrated schema is exercised without the ORM smoothing anything over.
_SEED_EXECUTION = (
    "INSERT INTO users (id, schema_version, name, email) "
    "VALUES ('u1', 1, 'Ada', 'ada@example.com')",
    "INSERT INTO projects (id, schema_version, user_id, title, description) "
    "VALUES ('p1', 1, 'u1', 'Caching', '')",
    "INSERT INTO pipeline_runs "
    "(id, schema_version, project_id, status, correlation_id, runtime_config, started_at) "
    "VALUES ('r1', 1, 'p1', 'running', 'c1', '{}', '2026-07-25 12:00:00')",
    "INSERT INTO stage_executions "
    "(id, schema_version, pipeline_run_id, stage, ordinal, status, correlation_id, started_at) "
    "VALUES ('e1', 1, 'r1', 'draft', 0, 'running', 'c1', '2026-07-25 12:00:00')",
)


def _config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def migrated_url(tmp_path: Path) -> str:
    """A database at head, built only by running the migrations."""
    url = f"sqlite+pysqlite:///{tmp_path / 'provenance.sqlite'}"
    command.upgrade(_config(url), "head")
    return url


def test_provenance_tables_upgrade_and_downgrade(tmp_path: Path, migrated_url: str) -> None:
    """Upgrading creates every provenance table; downgrading removes them all."""
    engine = create_engine(migrated_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert tables >= EXPECTED_TABLES, EXPECTED_TABLES - tables
    finally:
        engine.dispose()

    command.downgrade(_config(migrated_url), "base")
    engine = create_engine(migrated_url)
    try:
        remaining = set(inspect(engine).get_table_names())
        assert remaining == {"alembic_version"}
    finally:
        engine.dispose()


def test_the_migrated_schema_keeps_the_policy_version_check(migrated_url: str) -> None:
    """The CHECK travels with the migration, not just with the ORM metadata."""
    engine = create_engine(migrated_url)
    try:
        with engine.begin() as conn:
            for statement in _SEED_EXECUTION:
                conn.execute(text(statement))
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO decision_records "
                        "(id, schema_version, stage_execution_id, decision_type, decided_by,"
                        " decided_by_type, policy_version, inputs, outcome, rationale,"
                        " decided_at) "
                        "VALUES ('d1', 1, 'e1', 'route', 'routing-policy', 'policy', NULL,"
                        " '{}', 'rewrite', '', '2026-07-25 12:00:00')"
                    )
                )
    finally:
        engine.dispose()


def test_the_migrated_schema_keeps_the_trace_ordering_constraint(migrated_url: str) -> None:
    """Two events cannot claim the same position in one run's timeline."""
    engine = create_engine(migrated_url)
    insert = (
        "INSERT INTO trace_events "
        "(id, schema_version, event_type, timestamp, actor_type, actor_id, payload,"
        " correlation_id, sequence) "
        "VALUES (:id, 1, 'note', '2026-07-25 12:00:00', 'system', 'pipeline', '{{}}',"
        " 'c1', 0)"
    )
    try:
        with engine.begin() as conn:
            conn.execute(text(insert), {"id": "ev1"})
            with pytest.raises(IntegrityError):
                conn.execute(text(insert), {"id": "ev2"})
    finally:
        engine.dispose()
