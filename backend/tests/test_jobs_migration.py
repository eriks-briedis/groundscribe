"""Job-schema migration tests (phase 09).

The queue's hard guarantee — at most one live job per unit of work — is enforced
by the database, not only by :class:`~groundscribe.jobs.queue.JobQueue`. Two API
processes can pass the queue's own "is one already queued?" check at the same
instant, so a constraint the models declare but the migration omits would leave
a deployment without the guarantee the unit tests appear to prove. It is
re-checked here against a schema built only by running the migrations.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from groundscribe.db import create_engine

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"

#: A run to hang jobs from, written as raw SQL so the migrated schema is
#: exercised without the ORM smoothing anything over.
_SEED_RUN = (
    "INSERT INTO users (id, schema_version, name, email) "
    "VALUES ('u1', 1, 'Ada', 'ada@example.com')",
    "INSERT INTO projects (id, schema_version, user_id, title, description) "
    "VALUES ('p1', 1, 'u1', 'Caching', '')",
    "INSERT INTO pipeline_runs "
    "(id, schema_version, project_id, status, correlation_id, runtime_config, started_at) "
    "VALUES ('r1', 1, 'p1', 'running', 'c1', '{}', '2026-07-25 12:00:00')",
)

_INSERT_JOB = (
    "INSERT INTO jobs "
    "(id, schema_version, job_type, status, project_id, pipeline_run_id, dedupe_key,"
    " active_key, payload, result, attempts, max_attempts, created_at) "
    "VALUES (:id, 1, 'extract_source_model', :status, 'p1', 'r1', 'r1:extract',"
    " :active_key, '{}', '{}', 0, 1, '2026-07-25 12:00:00')"
)


def _config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def migrated_url(tmp_path: Path) -> str:
    """A database at head, built only by running the migrations."""
    url = f"sqlite+pysqlite:///{tmp_path / 'jobs.sqlite'}"
    command.upgrade(_config(url), "head")
    return url


def test_the_jobs_table_upgrades_and_downgrades(migrated_url: str) -> None:
    """The replacement table survives a round trip back to the phase-03 shell."""
    engine = create_engine(migrated_url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("jobs")}
        assert {"active_key", "dedupe_key", "heartbeat_at", "superseded_by_id"} <= columns
    finally:
        engine.dispose()

    command.downgrade(_config(migrated_url), "0012_validation")
    engine = create_engine(migrated_url)
    try:
        shell = {column["name"] for column in inspect(engine).get_columns("jobs")}
        assert shell == {
            "id",
            "schema_version",
            "job_type",
            "status",
            "pipeline_run_id",
            "payload",
            "created_at",
        }
    finally:
        engine.dispose()


def test_the_migrated_schema_allows_one_live_job_per_unit_of_work(migrated_url: str) -> None:
    """Duplicate prevention is a constraint, not a convention.

    The queue checks for an existing job before inserting, but two API processes
    can pass that check at the same instant. Only the database can settle it.
    """
    engine = create_engine(migrated_url)
    try:
        with engine.begin() as conn:
            for statement in _SEED_RUN:
                conn.execute(text(statement))
            conn.execute(
                text(_INSERT_JOB), {"id": "j1", "status": "pending", "active_key": "r1:extract"}
            )
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(_INSERT_JOB), {"id": "j2", "status": "pending", "active_key": "r1:extract"}
                )
    finally:
        engine.dispose()


def test_the_migrated_schema_frees_the_key_once_the_work_is_done(migrated_url: str) -> None:
    """Finished jobs release their key, so the same work may be run again.

    Several jobs may hold a null ``active_key`` at once — a unique constraint
    does not constrain nulls — which is what makes the history of repeated runs
    storable at all.
    """
    engine = create_engine(migrated_url)
    try:
        with engine.begin() as conn:
            for statement in _SEED_RUN:
                conn.execute(text(statement))
            conn.execute(text(_INSERT_JOB), {"id": "j1", "status": "succeeded", "active_key": None})
            conn.execute(text(_INSERT_JOB), {"id": "j2", "status": "succeeded", "active_key": None})
            conn.execute(
                text(_INSERT_JOB), {"id": "j3", "status": "pending", "active_key": "r1:extract"}
            )
    finally:
        engine.dispose()
