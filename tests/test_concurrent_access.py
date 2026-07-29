"""What a reader must not hold while it is reading (phase 09's shape, phase 11's screens).

Two processes share one database: the API serves screens while the worker writes
what the screens are about. That only works if reading is *finished* when the
answer has been sent — an HTTP request that leaves its transaction open, or a
progress stream that keeps one for as long as somebody is watching, holds the
database against the process trying to make progress.

It was invisible until the engine began transactions immediately (which it must,
or a read-then-write command is refused outright). That did not create these
leaks; it made them stop being harmless. The dashboard case is the one that
matters: a person opens a job's progress, the stream holds the connection, the
worker cannot commit the job, the job never finishes, and the stream never ends.

Both tests use a real file on disk and two engines, because that is the only
arrangement in which the claim means anything: one process cannot deadlock
against itself in a way a shared in-memory database would reproduce.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.db import Base, create_engine, session_factory
from groundscribe.domain import models as domain_models
from groundscribe.jobs.enums import JobType
from groundscribe.jobs.events import JobEventStream
from groundscribe.provenance import models
from groundscribe.provenance.enums import ExecutionStatus
from test_end_to_end import build_test_runtime


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "groundscribe.sqlite"


def open_database(path: Path) -> tuple[object, object]:
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, session_factory(engine)


def can_write(path: Path) -> bool:
    """Whether a *separate* connection can take the write lock right now."""
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    # Fail fast: the question is whether the lock is free, not how patient we are.
    with engine.connect() as probe:
        probe.exec_driver_sql("PRAGMA busy_timeout=250")
        try:
            probe.execute(text("CREATE TABLE IF NOT EXISTS probe (id INTEGER)"))
            probe.rollback()
            return True
        except Exception:
            return False
        finally:
            engine.dispose()


def test_a_read_leaves_no_transaction_open(database: Path, tmp_path: Path) -> None:
    """A screen that has been served is a screen that is no longer holding on."""
    engine, sessions = open_database(database)
    client = TestClient(
        create_app(runtime_factory=lambda: build_test_runtime(sessions(), tmp_path))
    )

    # 404, because nothing was created — the point is that it was *answered*.
    assert client.get("/projects/nope/dashboard").status_code == 404

    assert can_write(database), "the read is over; nothing should still hold the database"
    engine.dispose()  # type: ignore[attr-defined]


async def test_a_progress_stream_does_not_hold_the_database_while_it_waits(
    database: Path, tmp_path: Path
) -> None:
    """The deadlock this file exists for.

    A stream polls until the job it watches reaches a terminal state. Between
    polls it must let go: the process that will move that job is the one it is
    waiting for, and a stream that kept the connection would be waiting for
    something it had itself made impossible.
    """
    engine, sessions = open_database(database)
    session: Session = sessions()
    runtime = build_test_runtime(session, tmp_path)
    # The smallest graph a job can hang from: a run belongs to a project, which
    # belongs to an author.
    session.add(domain_models.User(id="u1", name="Ada", email="ada@example.com"))
    session.add(domain_models.Project(id="p1", user_id="u1", title="Watching"))
    session.add(
        models.PipelineRun(
            id="run-1",
            project_id="p1",
            status=ExecutionStatus.RUNNING,
            started_at=runtime.clock(),
            correlation_id="c1",
        )
    )
    session.flush()
    run = session.get(models.PipelineRun, "run-1")
    assert run is not None
    job = runtime.queue.enqueue(job_type=JobType.EXTRACT_SOURCE, run=run, payload={})
    session.commit()

    stream = JobEventStream(session, runtime.queue).stream(job.id)
    await anext(stream)  # the first frame: the job's current status

    # One poll in, still waiting for a worker that will never come.
    await asyncio.sleep(0.05)

    assert can_write(database), "a waiting stream must not hold the database"

    await stream.aclose()
    session.close()
    engine.dispose()  # type: ignore[attr-defined]
