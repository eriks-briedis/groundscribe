"""What a project's flagged material forbids the article from saying (phase 13).

plan/13 → *material flagged confidential / excluded-from-final-output cannot
appear in the publishable or exported article*.

Two enforcement points read this: final validation, which gives an author a named
finding to act on, and the export guard on the transition itself, which is the
one that cannot be forgotten. Both ask the same question of the same rows, so
they ask it through one function.

**Sentences, not whole segments.** A segment is a paragraph; an article that
borrowed one sentence from a confidential paragraph has leaked it just as
completely, and a check that looked for the paragraph entire would miss that.

**A length floor.** Restricting a sentence must not restrict the words in it. An
article that could not say "the cache was cold" because those words also appear
in a confidential paragraph would fail every time, and a check that fails every
time is one people learn to route around.

**The output boundary, not the classification.** Internal material may be sent to
a model and must not be printed. Asking "is this confidential?" instead of "may
this be published?" would let exactly that case through.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.confidentiality import Exclusion

#: The shortest span worth restricting, in characters.
#:
#: Long enough that a match is evidence of copying rather than of two people
#: describing the same system in English. Deliberately a constant a reader can
#: find and argue with, rather than a threshold buried in a comparison.
MINIMUM_SPAN = 40

#: Sentence boundaries, and the ends of lines. Crude on purpose: this splits
#: source prose into candidate spans, and a span that is slightly too long only
#: makes the check slightly harder to trip, never easier.
_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")


def restricted_spans(session: Session, project_id: str) -> tuple[str, ...]:
    """Every span of this project's material that may not be published.

    Deduplicated and ordered longest-first, so that when an article borrows a
    passage covered by two overlapping spans the finding names the larger one —
    which is the one that tells an author most about what they copied.
    """
    spans: set[str] = set()
    for text in _restricted_text(session, project_id):
        spans.update(_spans_of(text))
    return tuple(sorted(spans, key=lambda span: (-len(span), span)))


def _restricted_text(session: Session, project_id: str) -> list[str]:
    """The text of every segment and claim barred from the final output.

    Both tables, because both carry the flags: extraction can narrow a
    publishable paragraph into a claim that names a customer, and a check that
    only read segments would never see it.
    """
    segments = session.scalars(
        select(domain_models.SourceSegment)
        .join(
            domain_models.SourceDocument,
            domain_models.SourceSegment.document_id == domain_models.SourceDocument.id,
        )
        .where(domain_models.SourceDocument.project_id == project_id)
    ).all()
    claims = session.scalars(
        select(domain_models.SourceClaim).where(domain_models.SourceClaim.project_id == project_id)
    ).all()
    return [
        *(row.text for row in segments if row.flags.excludes(Exclusion.FINAL_OUTPUT)),
        *(row.text for row in claims if row.flags.excludes(Exclusion.FINAL_OUTPUT)),
    ]


def _spans_of(text: str) -> list[str]:
    """The sentences of ``text`` that are long enough to be evidence."""
    return [
        span for sentence in _BOUNDARY.split(text) if len(span := sentence.strip()) >= MINIMUM_SPAN
    ]


__all__ = ["MINIMUM_SPAN", "restricted_spans"]
