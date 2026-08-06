"""How an artefact is rendered into a prompt variable.

:mod:`groundscribe.stages.context` decides *which source segments* a stage may
show the model. This module decides how the artefacts built from them are spelled
once a stage has chosen to show one — a different question, and until now an
unasked one: every stage passed ``model_dump(mode="json")`` and let Jinja's
``{{ source_model }}`` call :func:`str` on the resulting dict.

That produced a Python dict literal on the wire, and three costs came with it.
Measured on one stored ``score_article`` request, whose source model rendered to
61,933 characters:

===========================================  =======
what                                          chars
===========================================  =======
as sent: ``str(dict)``, all 81 claims          61,933
claims only                                    53,230
…the 44 the draft declares it uses             28,021
…and without ``evidence[].segment_ids``        17,716
===========================================  =======

**Segment ids are 19.3% of it and nothing downstream reads them.** 527 literals
of the form ``'9b6c55b028054778b4a5e93c2220a7d5-37'``, each repeating the same
32-character document hash. They are checked once, by ``check_citations`` at
extraction, against the segment rows — never against the prompt. Every stage
after that was paying to ship an identifier it had no use for.

**A claim set is a contract, and each stage has one.** The brief is bound by the
architecture's allocation, the draft by the brief, and everything judging the
draft by what the draft declared it used. Sending all 81 claims to a stage whose
contract names 44 is not extra safety: material the architecture routed to
*another* article is scope drift when it appears in this one, which is a thing
`scope_discipline` exists to catch and cannot catch while the prose has been
invited to wander.

**JSON, not ``repr``.** Compact separators and double quotes, so the punctuation
tokenizes as JSON rather than as a Python literal.

The projection is a union, never an intersection: a caller passes every claim id
its contract names *and* every id the draft declared, so a claim the drafter used
without the brief naming it still reaches the reviewer. The alternative — showing
the judge less than the draft claims to rest on — would manufacture unsupported
claims, and an unsupported claim routes a ``factual_gap``, which is the one loop
in the policy with no round limit.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from typing import Any

from groundscribe.stages.schemas import SourceModel

#: Keys holding source-segment identifiers, wherever they appear in a dumped
#: source model. Named once because they sit at four different depths —
#: ``Evidence``, ``ProductFact``, ``DevelopmentEvent`` and
#: ``PublicationConstraint`` each carry one — and a list that missed a level
#: would silently keep paying for it.
_SEGMENT_KEYS = frozenset({"segment_ids"})


def source_model_payload(
    source_model: SourceModel,
    *,
    claim_ids: Collection[str] | None = None,
) -> str:
    """The source model as a stage should send it: compact, and no wider than the stage.

    ``claim_ids`` narrows ``claims`` to the ids named, and narrows the
    ``claim_ids`` references on lessons and potential arguments to match — a
    lesson keeps its statement either way, because the sentence is worth reading
    even when the claims behind it belong to another article. ``None`` keeps
    every claim, which is right for the stages reading the source as a whole
    rather than on behalf of one article.

    Unknown ids are ignored rather than raising. A draft naming a claim the model
    does not have is a real defect, and it is ``check_draft``'s to report, with
    the id in the message; failing here would replace that diagnosis with a
    stack trace from a serialiser.
    """
    payload = source_model.model_dump(mode="json")
    if claim_ids is not None:
        payload = _project(payload, frozenset(claim_ids))
    _strip_segment_ids(payload)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def claims_in_scope(*sources: Iterable[str] | None) -> frozenset[str]:
    """The union of every claim id a stage's contracts name.

    A convenience with a point: the union is the safe direction, and spelling it
    at each call site invites one of them to be written as an intersection.
    """
    scope: set[str] = set()
    for source in sources:
        if source is not None:
            scope.update(source)
    return frozenset(scope)


def _project(payload: dict[str, Any], keep: frozenset[str]) -> dict[str, Any]:
    """Narrow the claims, and the references to them, to ``keep``."""
    projected = dict(payload)
    projected["claims"] = [claim for claim in payload.get("claims", []) if claim.get("id") in keep]
    for field in ("lessons", "potential_arguments"):
        projected[field] = [
            {**item, "claim_ids": [ref for ref in item.get("claim_ids", []) if ref in keep]}
            for item in payload.get(field, [])
        ]
    return projected


def _strip_segment_ids(node: Any) -> None:
    """Drop every segment-id list, at whatever depth it sits. In place."""
    if isinstance(node, dict):
        for key in _SEGMENT_KEYS & node.keys():
            del node[key]
        for value in node.values():
            _strip_segment_ids(value)
    elif isinstance(node, list):
        for item in node:
            _strip_segment_ids(item)
