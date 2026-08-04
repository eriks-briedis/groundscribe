"""Provenance enumerations (phase 03).

The fixed vocabularies every execution record routes on. As in phase 02 these are
:class:`~enum.StrEnum`s so the stored value is a stable, human-readable string —
provenance dumps must stay legible years after the code that wrote them changed.

Because these values are written verbatim into persisted records, renaming a
member is a breaking change to history already on disk; the tests pin each
vocabulary exhaustively for exactly that reason.
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionStatus(StrEnum):
    """Lifecycle of a pipeline run or a stage execution.

    ``FAILED`` and ``CANCELLED`` are deliberately distinct: one is the system
    giving up, the other a human stopping the work. Both keep their partial
    trace (plan/03 → *Failure handling*, partial data preserved).
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActorType(StrEnum):
    """What kind of thing acted: who emitted an event or made a decision.

    ``POLICY`` is not a person — it is a versioned rule set, which is why a
    policy decision must also carry a ``policy_version``.
    """

    USER = "user"
    MODEL = "model"
    POLICY = "policy"
    TOOL = "tool"
    SYSTEM = "system"


class RetryType(StrEnum):
    """Why a follow-up model invocation was made.

    The spec (plan/03 → retry ordering) insists retries are *typed*: a bare
    attempt count cannot distinguish "the provider was rate-limiting us" from
    "the model kept emitting an invalid enum", and those demand different fixes.
    """

    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    PROVIDER_ERROR = "provider_error"
    INVALID_SCHEMA = "invalid_schema"
    CONTENT_REPAIR = "content_repair"
    MODEL_FALLBACK = "model_fallback"
    MANUAL = "manual"
    PROMPT_MODIFIED = "prompt_modified"


class InvocationOutcome(StrEnum):
    """How a single model invocation ended.

    ``INVALID_JSON`` and ``INVALID_SCHEMA`` are separate because a response can
    be useful yet unparseable, or parseable yet non-conforming; both are
    preserved alongside their repaired successor rather than discarded.

    ``TRUNCATED`` is separate from both for the same reason and a sharper one: a
    body the provider stopped mid-value parses as neither, and it is the only
    content outcome no retry can fix. The model did not answer badly — it was cut
    off — so the remedy is the stage's output budget, not another attempt.
    """

    ACCEPTED = "accepted"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    TRUNCATED = "truncated"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    RATE_LIMITED = "rate_limited"
    CANCELLED = "cancelled"


class ArtifactDirection(StrEnum):
    """Whether an execution consumed a snapshot or produced it."""

    INPUT = "input"
    OUTPUT = "output"


class ToolInitiator(StrEnum):
    """Who chose to call a tool: the model, or the pipeline itself."""

    MODEL_SELECTED = "model_selected"
    PIPELINE_MANDATED = "pipeline_mandated"


class ContextDisposition(StrEnum):
    """What happened to one context candidate during selection.

    Every candidate ends in exactly one of these states, so a context-selection
    record explains what the model *did not* see as well as what it did.
    """

    SELECTED = "selected"
    EXCLUDED = "excluded"
    TRUNCATED = "truncated"


class InterventionType(StrEnum):
    """The human control points at which a user steps into a run."""

    APPROVAL = "approval"
    REJECTION = "rejection"
    EDIT = "edit"
    OVERRIDE = "override"
    ANSWER = "answer"
    CANCELLATION = "cancellation"
    #: A person asked for failed work to be run again, spending another call.
    RETRY = "retry"
