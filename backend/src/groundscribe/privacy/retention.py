"""How much of a trace is kept, and for how long (phase 13).

plan/13 → *Trace-retention modes*: full / redacted-full /
metadata-and-structured-only / no-raw-provider-payloads /
temporary-raw-retention / minimal-operational-logging. Local-first may default to
detailed retention, but the choice is explicit.

**A mode governs payloads, never the record.** The invocation row — provider,
model, outcome, attempt ordinal, timings, tokens, cost — is written under every
mode, including the most restrictive. A trace that forgot a call happened would
not be a smaller trace; it would be a wrong one, and the cost, latency and
repair-rate numbers phase 12 computes from it would all be wrong with it.

**``full`` is not "unredacted".** Redaction before persistence is a product
principle (plan/00), not a retention setting, and nothing here can switch it off.
What ``full`` means is that every payload class is kept, indefinitely.

**``redacted_full`` is the one that goes past that floor**, removing the
project's own restricted source material from stored payloads as well. That is a
real difference from ``full``, and it is what someone asking for it wants: the
whole trace, minus the sensitive source.

**Expiry reads the mode recorded on the call**, not the project's current
setting. A settings change must not rewrite history that was captured under a
different promise — the same reason every artefact in this system records the
versions it ran under instead of looking them up later.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.retention import RetentionMode

#: The payload classes each mode permits to reach disk.
#:
#: A table rather than a chain of conditionals: this is the list a person has to
#: be able to read and check against the spec, and a rule spread over six
#: branches is a rule nobody can audit.
PERMITTED: dict[RetentionMode, frozenset[ArtifactType]] = {
    RetentionMode.FULL: frozenset(
        {
            ArtifactType.EFFECTIVE_REQUEST,
            ArtifactType.RAW_RESPONSE,
            ArtifactType.PARSED_RESPONSE,
            ArtifactType.VALIDATED_RESPONSE,
        }
    ),
    RetentionMode.REDACTED_FULL: frozenset(
        {
            ArtifactType.EFFECTIVE_REQUEST,
            ArtifactType.RAW_RESPONSE,
            ArtifactType.PARSED_RESPONSE,
            ArtifactType.VALIDATED_RESPONSE,
        }
    ),
    # Kept at first and swept later: a schema repair is diagnosed from the raw
    # text, and by the time the window closes that diagnosis has happened.
    RetentionMode.TEMPORARY_RAW_RETENTION: frozenset(
        {
            ArtifactType.EFFECTIVE_REQUEST,
            ArtifactType.RAW_RESPONSE,
            ArtifactType.PARSED_RESPONSE,
            ArtifactType.VALIDATED_RESPONSE,
        }
    ),
    # The prompt stays: a replay needs it, and it is material the project already
    # owned before any provider saw it.
    RetentionMode.NO_RAW_PROVIDER_PAYLOADS: frozenset(
        {
            ArtifactType.EFFECTIVE_REQUEST,
            ArtifactType.PARSED_RESPONSE,
            ArtifactType.VALIDATED_RESPONSE,
        }
    ),
    RetentionMode.METADATA_AND_STRUCTURED_ONLY: frozenset(
        {ArtifactType.PARSED_RESPONSE, ArtifactType.VALIDATED_RESPONSE}
    ),
    RetentionMode.MINIMAL_OPERATIONAL_LOGGING: frozenset(),
}

#: How long ``temporary_raw_retention`` keeps a raw provider payload.
#:
#: Long enough to diagnose a failure someone noticed the next working day; short
#: enough that "temporary" means something. Named so a deployment can argue with
#: the number rather than discover it.
DEFAULT_RAW_TTL = timedelta(days=7)


@dataclass(frozen=True)
class RetentionPolicy:
    """One project's retention choice, as the recorder reads it.

    The default is :attr:`RetentionMode.FULL` because a default is a choice
    nobody made: a trace can be thinned later and cannot be un-thinned.
    """

    mode: RetentionMode = RetentionMode.FULL
    #: Spans of this project's material to remove from stored payloads under
    #: ``redacted_full``. Ignored by every other mode — which is what makes the
    #: two "keep everything" modes genuinely different.
    restricted: Sequence[str] = field(default=())
    raw_payload_ttl: timedelta = DEFAULT_RAW_TTL

    def keeps(self, artifact_type: ArtifactType) -> bool:
        """Whether a payload of this class may be persisted at all."""
        return artifact_type in PERMITTED[self.mode]

    @property
    def extra_secrets(self) -> tuple[str, ...]:
        """Literals the redactor should remove on top of its usual rules."""
        if self.mode is not RetentionMode.REDACTED_FULL:
            return ()
        return tuple(span for span in self.restricted if span)


def expire_raw_payloads(
    session: Session,
    *,
    now: datetime,
    modes: Iterable[RetentionMode] = (RetentionMode.TEMPORARY_RAW_RETENTION,),
    ttl: timedelta = DEFAULT_RAW_TTL,
) -> int:
    """Drop raw provider payloads past their window; return how many.

    Only calls recorded under a mode that asked for expiry are touched, and the
    mode is read from the call rather than from the project. Sweeping by current
    settings would let a person shorten retention today and lose the payloads of
    a run captured last month under a promise to keep them.

    The reference is cleared; the invocation, its prompt, its structured output,
    its cost and its outcome all remain. Expiring a payload is not deleting a
    record.
    """
    # Imported here, not at module scope: the provenance models import
    # :class:`RetentionMode` from this module to type the column, and a top-level
    # import back would close the cycle. The dependency is genuinely one-way —
    # provenance knows the vocabulary, this module knows the policy — and only
    # the sweep needs the rows.
    from groundscribe.provenance import models

    wanted = {RetentionMode(mode) for mode in modes}
    cutoff = now - ttl
    invocations = session.scalars(
        select(models.ModelInvocation).where(
            models.ModelInvocation.raw_response_snapshot_id.is_not(None),
            models.ModelInvocation.started_at < cutoff,
        )
    ).all()

    expired = 0
    for invocation in invocations:
        if RetentionMode(invocation.retention_mode) not in wanted:
            continue
        invocation.raw_response_snapshot_id = None
        expired += 1
    if expired:
        session.flush()
    return expired


__all__ = [
    "DEFAULT_RAW_TTL",
    "PERMITTED",
    "RetentionMode",
    "RetentionPolicy",
    "expire_raw_payloads",
]
