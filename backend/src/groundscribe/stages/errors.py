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


__all__ = [
    "ArchitectureLocked",
    "BriefContractError",
    "EvidenceError",
    "ProviderNotPermitted",
    "StageError",
]
