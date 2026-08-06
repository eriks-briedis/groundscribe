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
from groundscribe.db import create_engine, read_only, session_factory
from groundscribe.jobs.queue import JobQueue
from groundscribe.llm.adapters.chatgpt import (
    CODEX_AUTH_FILE_ENV,
    ChatGPTClient,
    has_credentials,
)
from groundscribe.llm.adapters.ollama import OLLAMA_BASE_URL_ENV, OllamaClient
from groundscribe.llm.adapters.openai import OPENAI_API_KEY_ENV, OpenAIClient
from groundscribe.llm.generation import StructuredGenerator
from groundscribe.llm.pricing import PricingTable, default_pricing
from groundscribe.llm.protocol import LLMClient, RetryPolicy
from groundscribe.llm.routing import default_routing_policy
from groundscribe.paths import repo_root
from groundscribe.privacy.encryption import EncryptedBlobStore, KeyFileStore
from groundscribe.prompts import PromptStore, prompts_root
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.blob_store import BlobStorage, BlobStore
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.position import PositionStore

#: What a *running* installation waits before re-sending a failed call.
#:
#: :class:`RetryPolicy` defaults ``backoff_seconds`` to zero so the suite stays
#: fast, and every client was taking that default — which meant a rate-limited
#: stage re-sent its whole 45-60k-token prompt three times with no pause at all.
#: The number is small because the first retry should still be prompt; it doubles
#: per attempt, so three attempts span roughly six seconds rather than none.
#:
#: This is the only place the distinction can be drawn. The policy object cannot
#: default to a real wait without making every test that exercises a transport
#: failure sleep for it, and the tests are where that failure is exercised most.
SHIPPED_RETRY_POLICY = RetryPolicy(backoff_seconds=2.0)

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


def provider_clients(*, pricing: PricingTable | None = None) -> dict[str, LLMClient]:
    """Every provider this machine has been configured for. Possibly none.

    Configuration is what makes a provider *reachable*; it is not what makes it
    permitted. A project's ``allowed_providers`` allow-list (phase 13) still
    decides whether its material may go there, and it defaults to empty — so an
    installation configured for every provider here still sends nothing until an
    author names one. Two gates, two decisions, two people.

    Keyed by the provider string the routing config uses, because that is what the
    generator looks a client up by; registered under any other spelling it would
    never be found, and the failure would read as "no client for ollama" on a
    machine that had configured one.

    The three providers are configured by different *kinds* of fact, which is not
    an inconsistency but the difference between them: OpenAI needs a credential,
    Ollama needs an address, and ChatGPT needs a login someone else's tool
    performed. None is registered by default. Requiring ``OLLAMA_BASE_URL`` even
    though the adapter would default to localhost is the same decision plan/00
    made about silent network calls — a machine that happens to be running Ollama
    for something else has not thereby volunteered it to this application.

    ``chatgpt`` is that rule at its sharpest, and the reason it is a separate
    provider rather than a mode of ``openai``. Its credential is not this
    application's: it belongs to the Codex CLI, it is sitting in a well-known
    path on any machine whose owner has ever run ``codex login``, and finding it
    there says nothing about whether this pipeline was meant to spend it. So the
    file's presence registers the provider and nothing more — a project still has
    to name ``chatgpt`` in ``allowed_providers``, and a routing profile still has
    to send a stage there. Both are decisions a person makes, in that order.
    """
    table = pricing if pricing is not None else default_pricing()
    # A label, not a decision: the model each call uses comes from the stage's
    # route, and is what the invocation records. One client per provider is the
    # shape that keeps a single answer to "which model".
    fallback_model = default_routing_policy().default.primary.model
    clients: dict[str, LLMClient] = {}
    retry = SHIPPED_RETRY_POLICY

    if os.environ.get(OPENAI_API_KEY_ENV, "").strip():
        clients[OpenAIClient.provider] = OpenAIClient(
            model=fallback_model, pricing=table, retry_policy=retry
        )
    if os.environ.get(OLLAMA_BASE_URL_ENV, "").strip():
        clients[OllamaClient.provider] = OllamaClient(
            model=fallback_model, pricing=table, retry_policy=retry
        )
    if has_credentials():
        # Not ``fallback_model``: this backend serves exactly one model and
        # refuses every other id, so the label that would be a harmless
        # placeholder elsewhere would name something that cannot answer.
        clients[ChatGPTClient.provider] = ChatGPTClient(pricing=table, retry_policy=retry)
    return clients


@lru_cache(maxsize=1)
def reading_engine() -> Engine:
    """The same engine and pool, for transactions that only read.

    Cached alongside :func:`engine` because ``execution_options`` builds a new
    façade each call. It shares the pool, so this is a second view of one engine
    rather than a second set of connections.
    """
    return read_only(engine())


def build_runtime(*, clients: dict[str, LLMClient] | None = None, reading: bool = False) -> Runtime:
    """Assemble the application layer against the local installation.

    ``reading`` builds it for a request that will not write. On SQLite that is
    the difference between a screen that renders while a stage is running and one
    that waits fifteen seconds for a write lock it never wanted (KNOWN-ISSUES
    §1); on PostgreSQL it changes nothing, which is the point of asking for it
    here rather than in a dialect-aware branch further in.

    Providers are registered only when this machine has been configured for one —
    an OpenAI key, an Ollama address, or both. Nothing is registered by default,
    because a local-first tool that silently reached an external provider would be
    the opposite of what plan/00 promises, so a stage that needs a provider nobody
    configured fails loudly saying which one it wanted.
    """
    session = session_factory(reading_engine() if reading else engine())()
    blob_root = Path(os.environ.get(BLOB_ROOT_ENV) or repo_root() / "var" / "blobs")
    snapshots = SnapshotStore(session, blob_storage(blob_root))
    recorder = ProvenanceRecorder(session, snapshots)
    return Runtime(
        session=session,
        snapshots=snapshots,
        recorder=recorder,
        generator=StructuredGenerator(
            clients=clients if clients is not None else provider_clients(),
            recorder=recorder,
            prompts=PromptStore(prompts_root()),
            routing=default_routing_policy(),
        ),
        queue=JobQueue(session),
        positions=PositionStore(session),
    )


__all__ = [
    "BLOB_ROOT_ENV",
    "CODEX_AUTH_FILE_ENV",
    "DATABASE_URL_ENV",
    "DEFAULT_DATABASE_URL",
    "KEY_ROOT_ENV",
    "OLLAMA_BASE_URL_ENV",
    "OPENAI_API_KEY_ENV",
    "TRACE_ENCRYPTION_ENV",
    "blob_storage",
    "build_runtime",
    "engine",
    "provider_clients",
    "reading_engine",
]
