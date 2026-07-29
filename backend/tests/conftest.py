"""Shared pytest fixtures for the groundscribe backend test suite.

Provides the database test harness: a session-scoped in-memory engine whose
schema is created once, and a function-scoped session wrapped in an outer
transaction that is rolled back after each test. Because the session joins that
transaction via a savepoint, even a ``commit()`` inside a test is undone at
teardown — giving real transactional isolation rather than a fresh database per
test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# Importing the ORM modules registers their tables on ``Base.metadata``; every
# one of them is needed for the full schema to be visible.
import groundscribe.domain.models
import groundscribe.jobs.models
import groundscribe.provenance.models
import groundscribe.workflow.position  # noqa: F401
from groundscribe.db import Base, create_engine
from groundscribe.storage.blob_store import BlobStore
from groundscribe.storage.snapshot_store import SnapshotStore


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """A single in-memory SQLite engine with the full mapped schema created once."""
    eng = create_engine()
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
