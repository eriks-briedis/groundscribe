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
from groundscribe.privacy.encryption import EncryptedBlobStore, KeyFileStore
from groundscribe.prompts import PromptStore, prompts_root
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.blob_store import BlobStorage, BlobStore
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.position import PositionStore

#: Where the local database lives unless a deployment says otherwise.
DATABASE_URL_ENV = "GROUNDSCRIBE_DATABASE_URL"
DEFAULT_DATABASE_URL = "sqlite+pysqlite:///groundscribe.db"

#: Where content-addressed artefact bytes are written.
BLOB_ROOT_ENV = "GROUNDSCRIBE_BLOB_ROOT"

#: Set to turn on encryption at rest for stored artefact content (phase 13).
#:
#: Off by default, and that is a decision rather than an oversight: switching it
#: on for an installation that already has blobs would make every one of them
#: unreadable, which presents to a person as total data loss. It is a choice a
#: deployment makes, not a surprise it receives on upgrade.
TRACE_ENCRYPTION_ENV = "GROUNDSCRIBE_ENCRYPT_TRACES"

#: Where the encryption key lives, beside the blob root rather than inside it.
KEY_ROOT_ENV = "GROUNDSCRIBE_KEY_ROOT"


def blob_storage(blob_root: Path) -> BlobStorage:
    """The bytes store, encrypted if this deployment asked for it.

    The key root defaults to a sibling of the blob root, never a child. A key
    beside the data it protects is a lock taped to its own door: one copy of the
    blob directory would carry both halves.
    """
    blobs = BlobStore(blob_root)
    if not os.environ.get(TRACE_ENCRYPTION_ENV, "").strip():
        return blobs
    key_root = Path(os.environ.get(KEY_ROOT_ENV) or blob_root.parent / "keys")
    return EncryptedBlobStore(blobs, KeyFileStore(key_root))


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
    snapshots = SnapshotStore(session, blob_storage(blob_root))
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
    "KEY_ROOT_ENV",
    "TRACE_ENCRYPTION_ENV",
    "blob_storage",
    "build_runtime",
    "engine",
]
