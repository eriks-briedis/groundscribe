"""Shared construction helpers for the phase-03 provenance tests.

Not a conftest: keeping these in an ordinary module means an import error while
the subsystem is being built fails only the modules that use it, rather than
breaking collection for the whole suite.

The recorder is built with an injected clock and id factory so every test writes
deterministic timestamps and ids — provenance assertions are about exact stored
values, and ``uuid4``/wall-clock defaults would make them untestable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count

from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.privacy.retention import RetentionPolicy
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.redaction import Redactor
from groundscribe.storage.snapshot_store import SnapshotStore

#: The instant every test timeline starts from.
START = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def ticking_clock(step: timedelta = timedelta(seconds=1)) -> Callable[[], datetime]:
    """A clock that advances one fixed step per read, starting at :data:`START`."""
    ticks = count()
    return lambda: START + step * next(ticks)


def sequential_ids(prefix: str = "rec") -> Callable[[], str]:
    """An id factory producing ``prefix-1``, ``prefix-2``, … so ids are assertable."""
    counter = count(1)
    return lambda: f"{prefix}-{next(counter)}"


def seed_project(session: Session, *, user_id: str = "u1", project_id: str = "p1") -> str:
    """Persist the user and project a pipeline run needs, returning the project id."""
    session.add(domain_models.User(id=user_id, name="Ada", email="ada@example.com"))
    session.add(domain_models.Project(id=project_id, user_id=user_id, title="Caching write-up"))
    session.flush()
    return project_id


def make_recorder(
    session: Session,
    snapshots: SnapshotStore,
    *,
    redactor: Redactor | None = None,
    retention: RetentionPolicy | None = None,
) -> ProvenanceRecorder:
    """A recorder with a deterministic clock and id factory."""
    return ProvenanceRecorder(
        session,
        snapshots,
        redactor=redactor,
        clock=ticking_clock(),
        id_factory=sequential_ids(),
        retention=retention,
    )
