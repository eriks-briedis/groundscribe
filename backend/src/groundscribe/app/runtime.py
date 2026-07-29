"""What the application layer is built out of (phase 09).

One object holding the collaborators every command needs, assembled once per
process and passed down. Both interfaces — HTTP and CLI — construct a runtime and
then call the same service over it, which is what makes "the CLI shares the
service layer" a structural fact rather than a discipline.

The runtime is where the *fake* transport goes in tests and the real provider
adapters go in production. Nothing below it knows which it got, which is the
property that lets the whole editorial chain be exercised deterministically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from groundscribe.jobs.queue import JobQueue
from groundscribe.llm.generation import StructuredGenerator
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.policy import WorkflowPolicy, default_workflow_policy
from groundscribe.workflow.position import PositionStore


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Runtime:
    """Everything a command needs, and nothing a command should construct itself.

    ``actor_id`` names the system when no person is acting. Policy decisions are
    attributed to it, so it appears in decision records; "pipeline" is a poor
    name for a person and a good one for a rule set, which is the point.
    """

    session: Session
    snapshots: SnapshotStore
    recorder: ProvenanceRecorder
    generator: StructuredGenerator
    queue: JobQueue
    positions: PositionStore
    policy: WorkflowPolicy = field(default_factory=default_workflow_policy)
    actor_id: str = "pipeline"
    # Injected for the same reason the recorder's is: a record whose timestamp
    # cannot be controlled cannot be asserted on.
    clock: Callable[[], datetime] = _default_clock
    #: Whether this runtime is responsible for closing its session.
    #:
    #: True where the session was made for one request — every deployment — and
    #: false where a caller lent one it means to keep using, which is what the
    #: test harness does to see the same rows the application wrote. Closing a
    #: borrowed session would end a transaction its owner is still inside.
    owns_session: bool = True

    def release(self) -> None:
        """Finish with the database for now.

        Called when a request ends. A session holds its connection — and on
        SQLite the write lock — until its transaction ends, so leaving one open
        keeps the database against the worker until garbage collection gets to
        it.
        """
        if self.owns_session:
            self.session.close()


__all__ = ["Runtime"]
