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

Three of these are configuration tests and say so. The fourth stages the actual
failure with two threads, because it turned out to be deterministic once named:
a transaction that reads before it writes cannot upgrade its snapshot if anyone
committed in between, and SQLite refuses that upgrade without consulting the
busy timeout at all.
"""

from __future__ import annotations

import threading
import time
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


def test_a_transaction_that_reads_then_writes_is_not_refused(file_url: str) -> None:
    """The failure the two-process installation actually hits.

    Every command reads before it writes — resume the run, load its position,
    then record what happened. Under a *deferred* ``BEGIN`` that makes the
    transaction a reader first, and a reader that later wants to write must
    upgrade its snapshot. If anything committed in between, SQLite refuses the
    upgrade with ``database is locked`` **immediately**: this is the one busy
    case where the timeout is not consulted, because waiting could never help —
    the snapshot is already stale.

    Staged here with two threads, which is what the API and the worker are: one
    writes while the other is between its read and its write. Both should end up
    in the database, in whichever order the lock granted — delayed is fine,
    refused is not.
    """
    engine = create_engine(file_url)
    with engine.begin() as setup:
        setup.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    failures: list[Exception] = []
    reading = threading.Event()

    def other_process() -> None:
        """The worker: it writes as soon as it sees the command has started."""
        reading.wait(timeout=5)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("INSERT INTO t (id) VALUES (2)")
        except Exception as problem:
            failures.append(problem)

    worker = threading.Thread(target=other_process)
    worker.start()

    try:
        with engine.begin() as command:
            command.exec_driver_sql("SELECT count(*) FROM t")
            reading.set()
            # Long enough for the other side to get in between the read and the
            # write, which is the whole interleaving under test.
            time.sleep(0.3)
            command.exec_driver_sql("INSERT INTO t (id) VALUES (1)")
    except Exception as problem:
        failures.append(problem)

    worker.join(timeout=30)
    with engine.connect() as reader:
        rows = reader.exec_driver_sql("SELECT count(*) FROM t").scalar()
    engine.dispose()

    assert failures == [], f"both writes should have happened, in some order: {failures}"
    assert rows == 2


def test_the_configuration_survives_a_second_connection(file_url: str) -> None:
    """Every connection, not only the first.

    ``journal_mode`` is a property of the database and persists; ``busy_timeout``
    is per connection and does not. A pool that set it once would leave every
    connection after the first with the driver's default.

    One at a time, deliberately: transactions begin ``IMMEDIATE`` here, so two
    connections held open at once would be two writers, and the second would wait
    out the timeout for a lock the first has no intention of releasing. That is
    the setting working, not failing — but it makes a poor way to read a pragma.
    """
    engine = create_engine(file_url)

    with engine.connect() as first:
        first_timeout = first.exec_driver_sql("PRAGMA busy_timeout").scalar()
    with engine.connect() as second:
        second_timeout = second.exec_driver_sql("PRAGMA busy_timeout").scalar()

    assert first_timeout == second_timeout == BUSY_TIMEOUT_MS

    engine.dispose()
