"""The same pipeline, on the other database (phase 14).

plan/14 → *SQLite→Postgres parity: the domain avoids SQLite-specific behaviour;
the full test suite (or a designated integration subset) runs green against
Postgres too*, and plan/00 → *avoid SQLite-specific behaviour so migration stays
trivial*.

Every other test in this repository runs on in-memory SQLite. That is the right
default — it is fast, it needs nothing installed, and it is what a local-first
tool actually ships on. It is also the reason a Postgres divergence could sit
here undetected for thirteen phases: a suite that only ever runs on one database
cannot notice that it depends on it.

So this file runs the **designated integration subset** — a whole pipeline walk
to export, plus the provenance invariants that hang off it — against a real
PostgreSQL server, and asserts the same things about the result. Not a smaller
set of things: the point is *identical behaviour*, and a Postgres run checked
more loosely than the SQLite one would agree by not looking.

It skips, loudly, when no server is configured. A parity test that quietly
passed with nothing behind it would be worse than no parity test, because it
would be cited as evidence.

    docker run -d -p 55432:5432 -e POSTGRES_PASSWORD=groundscribe \\
        -e POSTGRES_USER=groundscribe -e POSTGRES_DB=groundscribe postgres:16-alpine
    GROUNDSCRIBE_TEST_POSTGRES_URL=postgresql+psycopg://groundscribe:groundscribe@localhost:55432/groundscribe \\
        uv run pytest tests/test_postgres_parity.py

The whole suite can be pointed at Postgres the same way, through
``GROUNDSCRIBE_TEST_DATABASE_URL`` — see ``backend/tests/db_fixtures.py``. This
file exists in addition to that, not instead of it: CI runs the subset on every
push, because a Postgres service container on every job is a cost the SQLite
default exists to avoid.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.db import Base, create_engine, session_factory
from groundscribe.domain import models as domain_models
from groundscribe.provenance import models as provenance_models
from groundscribe.storage.blob_store import BlobStore
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import Walkthrough
from service_helpers import Harness, build_harness
from test_pipeline_smoke import REQUIRED_STAGES

#: Where the parity server is. Absent means "no Postgres here", which is a skip.
POSTGRES_URL_ENV = "GROUNDSCRIBE_TEST_POSTGRES_URL"

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"{POSTGRES_URL_ENV} is not set: no PostgreSQL server to check parity against",
)


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    """A real PostgreSQL engine with the full mapped schema created on it.

    The schema is dropped and recreated per module rather than per test: the
    tests below each work inside a rolled-back transaction, so they cannot see
    each other, and recreating forty tables per test would make the parity run
    slow enough that nobody would run it.
    """
    engine = create_engine(os.environ[POSTGRES_URL_ENV])
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def pg_session(postgres_engine: Engine) -> Iterator[Session]:
    """A session rolled back afterwards, exactly as the SQLite fixture is."""
    connection = postgres_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def pg_harness(pg_session: Session, tmp_path: Path) -> Harness:
    return build_harness(pg_session, SnapshotStore(pg_session, BlobStore(tmp_path)))


@pytest.fixture
def pg_walk(pg_harness: Harness) -> Walkthrough:
    client = TestClient(create_app(runtime_factory=lambda: pg_harness.runtime))
    return Walkthrough(client, pg_harness)


def test_the_server_under_test_really_is_postgres(pg_session: Session) -> None:
    """The guard on everything below.

    A parity test pointed at SQLite by a mistyped URL would pass every assertion
    in this file and prove nothing at all, so the first thing it does is ask the
    server what it is.
    """
    assert pg_session.bind is not None
    assert pg_session.bind.dialect.name == "postgresql"
    version = pg_session.execute(text("select version()")).scalar_one()
    assert "PostgreSQL" in str(version)


async def test_the_whole_pipeline_runs_to_export_on_postgres(pg_walk: Walkthrough) -> None:
    """plan/14 → the designated integration subset, on the other database.

    The same walk ``test_pipeline_smoke`` runs, asserted the same way. If the
    domain had picked up a SQLite-ism — a datetime that arrives naive, an enum
    stored by name, a JSON column read back as a string — this is where it
    surfaces, because every one of those breaks a stage rather than a query.
    """
    await pg_walk.to_approval()
    published = await pg_walk.approve()
    assert published["state"] == "completed"

    exported = pg_walk.export()
    assert exported["version_id"] == pg_walk.validated_snapshot()
    assert exported["content"].startswith("# ")

    ran = {
        execution.stage
        for execution in pg_walk.session.scalars(select(provenance_models.StageExecution))
    }
    assert set(REQUIRED_STAGES) <= ran, f"never ran: {sorted(set(REQUIRED_STAGES) - ran)}"


async def test_the_provenance_invariant_holds_on_postgres_too(pg_walk: Walkthrough) -> None:
    """plan/00 → *every artefact references a creating execution*.

    Checked here as well as on SQLite because it is enforced partly by foreign
    keys, and foreign keys are exactly where the two databases differ by default:
    SQLite ignores them unless asked (phase 01 asks), Postgres never does. A
    schema that only held together because SQLite was not looking would fail
    here.
    """
    await pg_walk.to_approval()
    await pg_walk.approve()

    snapshots = list(pg_walk.session.scalars(select(domain_models.ArtifactSnapshot)))
    executions = {
        execution.id
        for execution in pg_walk.session.scalars(select(provenance_models.StageExecution))
    }

    assert snapshots
    assert [snapshot.id for snapshot in snapshots if not snapshot.created_by_execution_id] == []
    assert [
        snapshot.id for snapshot in snapshots if snapshot.created_by_execution_id not in executions
    ] == []


def test_a_timestamp_survives_the_round_trip_as_an_aware_instant(pg_session: Session) -> None:
    """The divergence phase 01 wrote ``UTCDateTime`` to close, checked on the
    side it was closed *for*.

    SQLite drops ``tzinfo`` on the way in and hands back a naive value; Postgres
    does neither. Provenance is a timeline compared across runs, so the same code
    recording different instants on the two backends would make every
    cross-database comparison quietly wrong. The type decorator normalises it —
    and this is the only test in the suite positioned to say the *Postgres* half
    of that is true.
    """
    run = provenance_models.PipelineRun(
        id="parity-run",
        project_id=_seed_project(pg_session),
        correlation_id="parity",
        runtime_config={"checked": True},
        started_at=datetime(2026, 7, 29, 12, 30, tzinfo=UTC),
    )
    pg_session.add(run)
    pg_session.flush()
    pg_session.expunge(run)

    stored = pg_session.get(provenance_models.PipelineRun, "parity-run")

    assert stored is not None
    assert stored.started_at == datetime(2026, 7, 29, 12, 30, tzinfo=UTC)
    assert stored.started_at.tzinfo is not None
    # And the JSON column is a mapping on the way out, not a string somebody has
    # to parse — the other portability trap in this schema.
    assert stored.runtime_config == {"checked": True}


def test_an_enum_is_stored_as_its_value_on_postgres(pg_session: Session) -> None:
    """plan/01 → enums render as ``VARCHAR`` holding the StrEnum *value*.

    Read back through raw SQL rather than the ORM, because the ORM would map
    either spelling back to the same member and agree with itself. What matters
    is the bytes on disk: a provenance dump has to stay legible years after the
    code that wrote it, and ``"succeeded"`` is legible where ``SUCCEEDED`` is a
    Python identifier that happens to have leaked.
    """
    project_id = _seed_project(pg_session)
    pg_session.add(
        provenance_models.PipelineRun(
            id="parity-enum",
            project_id=project_id,
            status=provenance_models.ExecutionStatus.SUCCEEDED,
            correlation_id="parity",
            started_at=datetime(2026, 7, 29, 12, 30, tzinfo=UTC),
        )
    )
    pg_session.flush()

    stored = pg_session.execute(
        text("select status from pipeline_runs where id = 'parity-enum'")
    ).scalar_one()

    assert stored == "succeeded"


def test_the_suite_can_be_pointed_at_this_server_wholesale() -> None:
    """plan/14 → *the full test suite ... runs green against Postgres too*.

    The subset above is what CI runs on every push; running *everything* against
    Postgres is one environment variable, and this asserts the switch exists
    rather than leaving it to a README claim nobody executes.
    """
    from db_fixtures import DATABASE_URL_ENV, configured_url

    assert DATABASE_URL_ENV == "GROUNDSCRIBE_TEST_DATABASE_URL"
    assert configured_url({}) == "sqlite+pysqlite:///:memory:"
    assert configured_url({DATABASE_URL_ENV: os.environ[POSTGRES_URL_ENV]}).startswith("postgresql")


def _seed_project(session: Session) -> str:
    """The user and project a pipeline run needs, under Postgres' foreign keys."""
    session.add(domain_models.User(id="parity-user", name="Ada", email="ada@example.com"))
    session.add(domain_models.Project(id="parity-project", user_id="parity-user", title="Parity"))
    session.flush()
    return "parity-project"


def _engine_for(url: str) -> Engine:
    """Named for readability at the call sites above."""
    engine = create_engine(url)
    session_factory(engine)
    return engine
