"""Shared construction helpers for the phase-09 job and worker tests.

Not a conftest, for the reason ``provenance_helpers`` is not one: an import
error while the subsystem is being built should fail the modules that use it
rather than break collection for the whole suite.

The queue is built with an injected clock and id factory. Leases, heartbeats and
requeues are decisions made by comparing timestamps, so a test that could not
move the clock could only assert them by sleeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from groundscribe.jobs.queue import JobQueue
from groundscribe.provenance import models
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import START, make_recorder, seed_project, sequential_ids


class ManualClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> datetime:
        self.now += delta
        return self.now


def make_queue(session: Session, clock: ManualClock | None = None) -> JobQueue:
    """A queue with a stoppable clock and assertable job ids."""
    return JobQueue(session, clock=clock or ManualClock(), id_factory=sequential_ids("job"))


def seed_run(
    session: Session,
    snapshots: SnapshotStore,
    recorder: ProvenanceRecorder | None = None,
) -> models.PipelineRun:
    """A project and an open pipeline run for jobs to hang from.

    A test that also records against the run must pass *its* recorder: two
    recorders built by ``make_recorder`` each run their own id counter, so
    sharing a database while not sharing a factory collides on the first write.
    """
    project_id = seed_project(session)
    return (recorder or make_recorder(session, snapshots)).start_run(project_id=project_id)


__all__ = ["ManualClock", "make_queue", "seed_run"]
