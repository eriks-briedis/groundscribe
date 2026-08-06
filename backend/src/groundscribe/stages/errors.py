"""What a stage refuses to do (phase 06).

These are failures of *editorial* correctness, not of transport or schema. The
phase-04 ladder already handles a model that returns malformed output; what is
left is output that is well-formed and still unusable — a citation pointing at a
passage that does not exist, an override of an architecture that was never
approved — plus the guards a stage applies before it calls anything at all.

They are exceptions rather than return values because every one of them means the
stage produced nothing: a caller cannot proceed with a partial source model, and a
nullable return would invite it to try.
"""

from __future__ import annotations


class StageError(Exception):
    """Base for editorial failures raised by a pipeline stage."""


class ProviderNotPermitted(StageError):
    """The project has not consented to this provider seeing its material.

    Raised before the call is made. A refusal that arrives once the material has
    crossed the wire has not protected anything (plan/00 → local-first by default,
    with visible data flow to external providers).
    """


class EvidenceError(StageError):
    """A structured output cites source material that is not in the source.

    Well-formed, schema-valid, and false. The repair ladder cannot see it because
    nothing about the response is malformed — only the stage knows which document
    it actually sent.
    """


class OverrideRejected(StageError):
    """An architecture edit named something the proposal does not contain.

    Its own type so the message reaches the author. It says which id was not
    found, which is exactly what someone who has just clicked *remove* needs —
    raised as a bare ``ValueError`` it was unmapped, and a request that could
    have explained itself answered "500" instead.
    """


class ArchitectureLocked(StageError):
    """An approved architecture was changed without an override naming who did it."""


class BriefContractError(StageError):
    """A brief would license the draft to do something the source model forbids.

    Distinct from :class:`EvidenceError`, which is about references that point
    nowhere. This is about references that point somewhere and then *drop what
    they found*: a qualification the source demanded, a constraint on what may be
    published, a length the project set. The brief is what phase 07 writes against
    and phase 08 validates against, so a clause missing here is a clause nothing
    downstream will ever check.
    """


class DraftContractError(StageError):
    """A draft broke a promise the brief or the source model made on its behalf.

    The prose-level sibling of :class:`BriefContractError`: a qualification the
    source demanded and the draft stated flat, or material the brief excluded and
    the draft printed. Both read as perfectly good English, which is why nothing
    downstream would catch them.
    """


class PlanContractError(StageError):
    """A revision plan would send the rewriter off with the wrong instructions.

    plan/07 names the risk directly: a rewriter that blindly applies reviewer
    suggestions. A plan that dropped a finding the author accepted, or promised to
    preserve a claim that does not exist, is that failure one stage earlier — and
    one stage earlier is where it can still be caught.
    """


class RewriteContractError(StageError):
    """A rewrite departed from the plan it was given.

    Distinct from :class:`DraftContractError`, which is about promises the *brief*
    and the source model make. This is about the promises the revision plan makes:
    that the required changes are applied, and that the claims it protected are
    still argued. Both are ways of quietly overriding a decision a person made.
    """


class VoiceRuleViolation(StageError):
    """The finished prose breaks a hard rule of the voice profile (phase 10).

    Distinct from :class:`VoiceContractError`, which is the pass misreporting
    *itself*. This one is a pass that reported honestly and still wrote something
    the author said must never appear.

    It stops the article rather than being corrected in place, because there is
    nothing else honest to do: rewriting the sentence is the model's job and has
    just been attempted, and letting the version through would publish exactly
    what the rule exists to prevent. The execution keeps its trace, so the
    rejected prose is available to whoever decides what happens next.
    """


class VoiceContractError(StageError):
    """A style pass did something a style pass may not do.

    The prohibited *changes* are unrepresentable by construction — the voice schema
    has no field for a claim or a thesis — so what is left is a pass misreporting
    itself: an edit whose "before" was never in the prose, an edit whose "after" is
    not in the result, or a marker quietly deleted. Each publishes something the
    record says is not there.
    """


class ScoreContractError(StageError):
    """A score would be stored that nothing could compare against.

    Scores are only useful in relation to other scores — did this rewrite improve
    on its parent, did the rubric change or did the article get worse — and every
    one of those questions needs the score to name the article, the inputs, the
    rubric and the thresholds behind it. A score missing any of them is not a
    weaker record, it is one that will be compared against anyway.
    """


__all__ = [
    "ArchitectureLocked",
    "BriefContractError",
    "DraftContractError",
    "EvidenceError",
    "PlanContractError",
    "ProviderNotPermitted",
    "RewriteContractError",
    "ScoreContractError",
    "StageError",
    "VoiceContractError",
    "VoiceRuleViolation",
]
