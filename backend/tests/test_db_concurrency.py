"""Two processes over one SQLite file (phase 09's shape, configured in phase 11).

plan/00 → *SQLite for local dev, PostgreSQL for concurrent/long-term*. plan/09 →
the worker is a **separate process** from the API, over a DB-backed queue.

Those two sentences meet on the default local installation, where the API and the
worker hold the same file open: the API writes for the length of a command while
the worker polls the queue. Under SQLite's rollback journal a commit takes an
exclusive lock on the whole database, and a reader that arrives during one is
refused rather than delayed — which is how running the two together surfaces as
``database is locked`` in whichever process asked second.

Write-ahead logging removes that: readers never block on a writer, and a second
writer waits out the busy timeout instead of failing at once. Both are things
Postgres already does, which is the point — nothing above the engine has to know
which database it is on.

These are configuration tests, and they say so. The failure they prevent is a
race between two processes committing, which is real but not something a
deterministic test can stage; what *can* be pinned is that the engine is
configured so the race has no teeth.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from groundscribe.db import BUSY_TIMEOUT_MS, create_engine


def pragma(engine: Engine, name: str) -> str:
    with engine.connect() as connection:
        return str(connection.exec_driver_sql(f"PRAGMA {name}").scalar())


@pytest.fixture
def file_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'groundscribe.db'}"


def test_a_file_database_journals_ahead_of_itself(file_url: str) -> None:
    """So the worker's poll is never refused by a request that is committing."""
    engine = create_engine(file_url)

    assert pragma(engine, "journal_mode").lower() == "wal"

    engine.dispose()


def test_a_second_writer_waits_rather_than_failing_immediately(file_url: str) -> None:
    """Write-ahead logging still allows one writer at a time.

    The timeout is set explicitly rather than left to the driver's five seconds:
    a stage commits its provenance — trace events, invocations, artefacts — in
    one transaction, and the request that collides with it should wait for that
    rather than report a locked database to a person who did nothing wrong.
    """
    engine = create_engine(file_url)

    assert int(pragma(engine, "busy_timeout")) == BUSY_TIMEOUT_MS

    engine.dispose()


def test_an_in_memory_database_is_left_alone() -> None:
    """Nothing is asked of the test database that it cannot do.

    Write-ahead logging needs a file to write ahead into, and the one connection
    the suite shares cannot contend with itself.
    """
    engine = create_engine("sqlite+pysqlite://")

    assert pragma(engine, "journal_mode").lower() == "memory"

    engine.dispose()


def test_the_configuration_survives_a_second_connection(file_url: str) -> None:
    """Every connection, not only the first.

    ``journal_mode`` is a property of the database and persists; ``busy_timeout``
    is per connection and does not. A pool that set it once would leave every
    connection after the first with the driver's default.
    """
    engine = create_engine(file_url)

    with engine.connect() as first, engine.connect() as second:
        first_timeout = first.exec_driver_sql("PRAGMA busy_timeout").scalar()
        second_timeout = second.exec_driver_sql("PRAGMA busy_timeout").scalar()

    assert first_timeout == second_timeout == BUSY_TIMEOUT_MS

    engine.dispose()
