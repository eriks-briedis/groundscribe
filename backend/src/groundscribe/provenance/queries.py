"""Reading provenance back out (phase 03).

The recorder's counterpart. A provenance system is only as good as the questions
it can answer, so the reconstructions the spec names — "what request was actually
sent?", "in what order and why were the attempts made?" — are functions here
rather than ad-hoc queries scattered through callers.
"""

from __future__ import annotations

import json

from groundscribe.provenance.models import ModelInvocation
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
