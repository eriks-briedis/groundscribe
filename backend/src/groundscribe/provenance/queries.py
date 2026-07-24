"""Reading provenance back out (phase 03).

The recorder's counterpart. A provenance system is only as good as the questions
it can answer, so the reconstructions the spec names — "what request was actually
sent?", "in what order and why were the attempts made?" — are functions here
rather than ad-hoc queries scattered through callers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.provenance.models import ModelInvocation, StageExecution, TraceEvent
from groundscribe.provenance.schemas import EffectiveRequest
from groundscribe.storage.snapshot_store import SnapshotStore


def reconstruct_effective_request(
    snapshots: SnapshotStore, invocation: ModelInvocation
) -> EffectiveRequest:
    """Rebuild the exact (redacted) request that produced ``invocation``.

    Reads the stored snapshot rather than re-rendering the template: a template
    that has since changed would re-render into a different request, which is
    precisely the confusion provenance exists to prevent.
    """
    snapshot = invocation.request_snapshot
    if snapshot is None:
        raise ValueError(f"invocation {invocation.id} has no stored effective request")
    return EffectiveRequest.model_validate(json.loads(snapshots.read(snapshot)))


def attempt_chain(root: ModelInvocation) -> list[ModelInvocation]:
    """The ordered attempt chain rooted at ``root``, first attempt first.

    Depth-first by ``attempt_ordinal``, so a linear repair chain reads in the
    order it happened and a branching one keeps each branch contiguous.
    """
    chain = [root]
    for child in sorted(root.attempts, key=lambda attempt: attempt.attempt_ordinal):
        chain.extend(attempt_chain(child))
    return chain


@dataclass(frozen=True)
class BranchComparison:
    """The lineage context of two executions being compared.

    Each side's parent is reported from its *own* lineage — the tempting error is
    to describe one branch in terms of the other, which reads plausibly and
    misattributes every difference between them.
    """

    left_execution_id: str
    right_execution_id: str
    left_parent_id: str | None
    right_parent_id: str | None
    common_ancestor_id: str | None


def _ancestry(session: Session, execution: StageExecution) -> list[str]:
    """Ids from ``execution`` up to its root, nearest first."""
    chain: list[str] = [execution.id]
    current = execution.parent_execution_id
    while current is not None and current not in chain[:-1]:
        chain.append(current)
        parent = session.get(StageExecution, current)
        current = parent.parent_execution_id if parent is not None else None
    return chain


def compare_executions(
    session: Session, left: StageExecution, right: StageExecution
) -> BranchComparison:
    """Compare two executions, reporting each side's true parent and their ancestor.

    Executions from unrelated runs compare cleanly to "nothing in common" rather
    than raising: asking is legitimate, and a null answer is the answer.
    """
    left_line = _ancestry(session, left)
    right_line = _ancestry(session, right)
    shared = next((node for node in right_line if node in left_line), None)
    return BranchComparison(
        left_execution_id=left.id,
        right_execution_id=right.id,
        left_parent_id=left.parent_execution_id,
        right_parent_id=right.parent_execution_id,
        common_ancestor_id=shared,
    )


def timeline(session: Session, correlation_id: str) -> list[TraceEvent]:
    """Every trace event of one run, in stored sequence order.

    Ordered by ``sequence`` rather than by ``timestamp``: the sequence is a
    stored total order, while timestamps can tie at the clock's resolution and
    would leave the reading of a timeline dependent on the machine that wrote it.
    """
    stmt = (
        select(TraceEvent)
        .where(TraceEvent.correlation_id == correlation_id)
        .order_by(TraceEvent.sequence)
    )
    return list(session.execute(stmt).scalars())


def causal_path(session: Session, event: TraceEvent) -> list[TraceEvent]:
    """The chain of causes ending at ``event``, root first.

    Follows ``causation_id`` rather than sequence: "what happened before this"
    and "what triggered this" are different questions, and only the second
    explains anything.
    """
    path = [event]
    seen = {event.id}
    current = event
    while current.causation_id is not None:
        cause = session.get(TraceEvent, current.causation_id)
        # A missing or looping cause stops the walk rather than failing: a
        # partial explanation is still worth returning to whoever is debugging.
        if cause is None or cause.id in seen:
            break
        path.append(cause)
        seen.add(cause.id)
        current = cause
    return list(reversed(path))
