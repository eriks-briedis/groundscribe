"""The job subsystem's vocabularies (phase 09).

Kept here rather than beside the provenance enums for the reason the model is
kept here too: a job's lifecycle is not an execution's. An execution succeeds,
fails or is cancelled; a job can also be *superseded* — chosen against before it
ever ran — which is a fact about queueing, meaningless to a stage that has
already started.

Both are :class:`~enum.StrEnum`s and both are persisted verbatim, so renaming a
member rewrites the meaning of rows already on disk.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    """Where a queued unit of work has got to.

    ``SUPERSEDED`` is the member that earns this enum its own existence: a job
    replaced by a newer command is neither cancelled (nobody stopped it) nor
    failed (nothing went wrong). It is work the system decided not to do, and
    saying so is the difference between a queue that can be audited and one that
    silently drops requests.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"

    @property
    def is_terminal(self) -> bool:
        """Whether no worker will touch this job again."""
        return self in _TERMINAL


_TERMINAL = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SUPERSEDED}
)


class JobType(StrEnum):
    """The units of work a worker knows how to run.

    A closed set, because the worker dispatches on it: an unknown job type must
    be a loud failure at enqueue time rather than a job that sits pending
    forever because nothing is registered to run it.

    Only stages that call a model are here. Deterministic work — ingesting a
    source, validating a finished article, approving anything — runs inside the
    request that asked for it, since queueing microseconds of local computation
    would add a round trip and a second failure mode and buy nothing.

    Answering a source question has no member of its own: an answer re-enters
    *extraction* (plan/06 → the source model is rebuilt, not patched), so it
    enqueues :attr:`EXTRACT_SOURCE_MODEL` carrying the answers.
    """

    EXTRACT_SOURCE_MODEL = "extract_source_model"
    PROPOSE_ARCHITECTURE = "propose_architecture"
    GENERATE_BRIEF = "generate_brief"
    GENERATE_DRAFT = "generate_draft"
    REVIEW_ARTICLE = "review_article"
    PLAN_REVISION = "plan_revision"
    REWRITE_ARTICLE = "rewrite_article"
    CORRECT_CLAIMS = "correct_claims"
    ALIGN_VOICE = "align_voice"
    SCORE_ARTICLE = "score_article"


__all__ = ["JobStatus", "JobType"]
