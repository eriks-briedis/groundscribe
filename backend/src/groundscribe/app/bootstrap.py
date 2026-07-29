"""Assembling a runtime against the local installation (phase 09).

Both front doors need the same object graph — a session, a snapshot store, a
recorder, a generator, a queue and a position store — and neither of them should
be the place that knows how to build it. A CLI that constructed its own runtime
would drift from the one the API serves, and the two would eventually disagree
about where artefacts are stored or which routing policy is in force.

Configuration is environment-driven so the same binary can be pointed at a
different database or blob root without a code change (plan/00 → local-first, and
plan/14's deployment work builds on this).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy.engine import Engine

from groundscribe.app.runtime import Runtime
from groundscribe.db import create_engine, session_factory
from groundscribe.jobs.queue import JobQueue
from groundscribe.llm.generation import StructuredGenerator
from groundscribe.llm.protocol import LLMClient
from groundscribe.llm.routing import default_routing_policy
from groundscribe.paths import repo_root
from groundscribe.prompts import PromptStore, prompts_root
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.blob_store import BlobStore
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.position import PositionStore

#: Where the local database lives unless a deployment says otherwise.
DATABASE_URL_ENV = "GROUNDSCRIBE_DATABASE_URL"
DEFAULT_DATABASE_URL = "sqlite+pysqlite:///groundscribe.db"

#: Where content-addressed artefact bytes are written.
BLOB_ROOT_ENV = "GROUNDSCRIBE_BLOB_ROOT"


@lru_cache(maxsize=1)
def engine() -> Engine:
    """The process's database engine, built once.

    Cached because an engine owns a connection pool: building one per request
    would open and discard a pool per command, which is the usual way a working
    application becomes a slow one.
    """
    return create_engine(os.environ.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL))


def build_runtime(*, clients: dict[str, LLMClient] | None = None) -> Runtime:
    """Assemble the application layer against the local installation.

    No provider clients are registered by default. Wiring real adapters is a
    deployment decision (phase 14), and a local-first tool that silently reached
    an external provider would be the opposite of what plan/00 promises — so a
    stage that needs one fails loudly saying which provider it wanted.
    """
    session = session_factory(engine())()
    blob_root = Path(os.environ.get(BLOB_ROOT_ENV) or repo_root() / "var" / "blobs")
    snapshots = SnapshotStore(session, BlobStore(blob_root))
    recorder = ProvenanceRecorder(session, snapshots)
    return Runtime(
        session=session,
        snapshots=snapshots,
        recorder=recorder,
        generator=StructuredGenerator(
            clients=clients or {},
            recorder=recorder,
            prompts=PromptStore(prompts_root()),
            routing=default_routing_policy(),
        ),
        queue=JobQueue(session),
        positions=PositionStore(session),
    )


__all__ = [
    "BLOB_ROOT_ENV",
    "DATABASE_URL_ENV",
    "DEFAULT_DATABASE_URL",
    "build_runtime",
    "engine",
]
