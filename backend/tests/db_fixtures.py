"""The database and storage fixtures, in a module both suites can import.

Named rather than left in a ``conftest``: the cross-cutting tests under
``tests/`` need the same harness the backend suite uses, and two files called
``conftest`` cannot both be imported as plugins. Putting the fixtures somewhere
with a name of their own lets both directories re-export them instead of one
growing a second, quietly different setup.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# Importing the ORM modules registers their tables on ``Base.metadata``; every
# one of them is needed for the full schema to be visible.
import groundscribe.domain.models
import groundscribe.experiments.models
import groundscribe.jobs.models
import groundscribe.provenance.models
import groundscribe.voice.models
import groundscribe.workflow.position  # noqa: F401
from groundscribe.db import DEFAULT_URL, Base, create_engine
from groundscribe.storage.blob_store import BlobStore
from groundscribe.storage.snapshot_store import SnapshotStore

#: Points the whole suite at another database (plan/14 → SQLite↔Postgres parity).
#:
#: In-memory SQLite stays the default because it is what a local-first tool ships
#: on and it needs nothing installed. But a suite that *can only* run on one
#: database cannot notice that it depends on it, so the switch exists — and
#: ``tests/test_postgres_parity.py`` asserts that it does, rather than leaving it
#: to a README claim nobody executes.
DATABASE_URL_ENV = "GROUNDSCRIBE_TEST_DATABASE_URL"


def configured_url(environ: Mapping[str, str] | None = None) -> str:
    """The database this run should use: the environment's, or in-memory SQLite."""
    source = environ if environ is not None else os.environ
    return source.get(DATABASE_URL_ENV, "").strip() or DEFAULT_URL


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """One engine with the full mapped schema, created once.

    In-memory SQLite unless :data:`DATABASE_URL_ENV` names something else. The
    schema is dropped first when it does: a server-backed database persists
    between runs, and a previous run's tables are a different starting state
    from the empty one every test in this suite assumes.
    """
    url = configured_url()
    eng = create_engine(url)
    if url != DEFAULT_URL:
        Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """A session bound to an outer transaction that is rolled back after the test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def blob_store(tmp_path: Path) -> BlobStore:
    """A content-addressed blob store rooted in this test's temporary directory."""
    return BlobStore(tmp_path)


@pytest.fixture
def snapshot_store(db_session: Session, blob_store: BlobStore) -> SnapshotStore:
    """The phase-02 snapshot store bound to the rolled-back test session."""
    return SnapshotStore(db_session, blob_store)


__all__ = [
    "DATABASE_URL_ENV",
    "blob_store",
    "configured_url",
    "db_session",
    "engine",
    "snapshot_store",
]
