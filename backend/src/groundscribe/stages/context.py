"""Choosing what a stage is allowed to show the model (phase 06, extended phase 12).

Phase 06 selected source segments one way — in the order they were written, until
the budget ran out — and lived inside the extraction stage, because there was
nothing else it could have been. plan/12 adds a second way and, with it, the
reason this is its own module: once selection has a *choice*, the choice has to
be nameable, versioned, and recorded, or two runs of the same stage over the same
source can differ with nothing in the trace to say why.

**Two strategies, both closed and versioned.**

- ``source_segments_in_order`` reads the source as written and truncates at the
  budget. It is the default and stays the default. Extraction is asked to recover
  a development history; the order of the material *is* part of that history.
- ``relevance_ranked_source_segments`` ranks the segments against a query and
  fills the budget best-first. It is what plan/12 means by retrieval.

**Ranking is conditional, inside the strategy itself.** A source that fits its
budget is sent whole even under the ranked strategy, and the record says so. This
is plan/12's stated risk — *adding retrieval "because it's common"* — answered
where it cannot be forgotten: choosing the strategy does not commit a run to
paying for relevance it does not need.

**Full-text, not embeddings.** BM25 over the segments, computed here, with no
provider call and no index to keep in step with the source. plan/12 lists
embedding and hybrid retrieval as options rather than requirements, and an
embedding strategy would send the entire source to a provider in order to decide
what may be sent to a provider — which is a data-flow decision, not a ranking
one, and belongs to the phase that has a reason to make it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from groundscribe.domain.confidentiality import Exclusion
from groundscribe.domain.models import SourceSegment
from groundscribe.provenance.enums import ContextDisposition
from groundscribe.provenance.schemas import ContextCandidate

#: Characters per token. A heuristic, and deliberately a crude one: it is used to
#: decide what fits, and it is *recorded* alongside the decision, so a later phase
#: swapping in a real tokeniser changes the numbers without changing the meaning.
CHARS_PER_TOKEN = 4

#: BM25's term-frequency saturation and length-normalisation constants, at the
#: values the literature settles on. Named rather than inlined because a ranking
#: nobody can reproduce is a ranking nobody can argue with.
BM25_K1 = 1.5
BM25_B = 0.75

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")


class ContextStrategy(StrEnum):
    """How a stage decides what to show the model.

    The values are the strings written into the context-selection record, so the
    enum and the provenance share one vocabulary. A strategy added here without a
    version below fails at import rather than at selection time.
    """

    IN_ORDER = "source_segments_in_order"
    RELEVANCE_RANKED = "relevance_ranked_source_segments"


#: The version of each strategy's *behaviour*. Bumped when the same input would
#: now be selected differently — which is the only thing a reader of an old
#: record needs the number for.
STRATEGY_VERSIONS: dict[ContextStrategy, str] = {
    ContextStrategy.IN_ORDER: "1",
    ContextStrategy.RELEVANCE_RANKED: "1",
}


@dataclass(frozen=True)
class SelectedSegment:
    """One segment as offered to the model, possibly shortened to fit."""

    segment: SourceSegment
    text: str
    truncated: bool

    @property
    def id(self) -> str:
        return self.segment.id

    @property
    def kind(self) -> str:
        return self.segment.kind.value


@dataclass(frozen=True)
class ContextWindow:
    """What was chosen for one call, and what became of everything else.

    ``ranked`` says whether relevance was actually computed. It is not the same
    question as which strategy ran: the ranked strategy over a source that fits
    ranks nothing, and a reader who inferred "scored" from "strategy" would read
    an absent score as a zero.
    """

    selected: tuple[SelectedSegment, ...]
    candidates: tuple[ContextCandidate, ...]
    token_budget: int
    strategy: ContextStrategy = ContextStrategy.IN_ORDER
    ranked: bool = False

    @property
    def strategy_version(self) -> str:
        return STRATEGY_VERSIONS[self.strategy]


def select_context(
    segments: Sequence[SourceSegment],
    *,
    strategy: ContextStrategy = ContextStrategy.IN_ORDER,
    query: str = "",
    token_budget: int,
) -> ContextWindow:
    """Fit the source into ``token_budget`` under the named strategy.

    ``query`` is what relevance is measured against and is ignored by the
    in-order strategy. It is accepted by both so that switching strategy is a
    change of one argument — a caller that had to restructure its call to run an
    experiment would be a caller that quietly stops running them.

    Material flagged excluded-from-model-input is withheld first, whatever the
    strategy (plan/13).
    """
    sendable, withheld = _partition_by_confidentiality(segments)
    if strategy is ContextStrategy.RELEVANCE_RANKED:
        window = _relevance_ranked(sendable, query=query, token_budget=token_budget)
    else:
        window = _in_order(sendable, token_budget=token_budget)
    return _with_withheld(window, segments, withheld)


def select_segments(segments: Sequence[SourceSegment], *, token_budget: int) -> ContextWindow:
    """Phase 06's selection, by the name phase 06 gave it."""
    return select_context(segments, token_budget=token_budget)


# ----------------------------------------------------------------------
# Confidentiality (phase 13)
# ----------------------------------------------------------------------


def _partition_by_confidentiality(
    segments: Sequence[SourceSegment],
) -> tuple[list[SourceSegment], dict[str, ContextCandidate]]:
    """Split the source into what may be sent and what may not.

    Done *before* the strategy runs, so withheld material never competes for the
    budget. A confidential paragraph that still reserved its own space would push
    publishable material out of the prompt, which would make marking something
    confidential quietly degrade the article — a cost nobody agreed to and
    nothing in the record would explain.
    """
    sendable: list[SourceSegment] = []
    withheld: dict[str, ContextCandidate] = {}
    for segment in segments:
        flags = segment.flags
        if flags.may_be_sent_to_a_provider:
            sendable.append(segment)
            continue
        withheld[segment.id] = ContextCandidate(
            reference=segment.id,
            disposition=ContextDisposition.EXCLUDED,
            # Names confidentiality and never the budget: a reader who cannot
            # tell "withheld because it is confidential" from "did not fit"
            # cannot tell a working safeguard from a small budget.
            reason=(
                f"withheld from the model: {flags.classification.value} material, "
                f"{Exclusion.MODEL_INPUT.value}"
            ),
        )
    return sendable, withheld


def _with_withheld(
    window: ContextWindow,
    segments: Sequence[SourceSegment],
    withheld: Mapping[str, ContextCandidate],
) -> ContextWindow:
    """Put the withheld segments back into the record, in document order.

    They are absent from ``selected`` and present in ``candidates``. Material
    that vanished without a record would be indistinguishable from material that
    was never there — the confusion the context-selection record exists to
    prevent (plan/03).
    """
    if not withheld:
        return window
    considered = {candidate.reference: candidate for candidate in window.candidates}
    return ContextWindow(
        selected=window.selected,
        candidates=tuple(
            withheld.get(segment.id) or considered[segment.id]
            for segment in segments
            if segment.id in withheld or segment.id in considered
        ),
        token_budget=window.token_budget,
        strategy=window.strategy,
        ranked=window.ranked,
    )


# ----------------------------------------------------------------------
# In order
# ----------------------------------------------------------------------


def _in_order(segments: Sequence[SourceSegment], *, token_budget: int) -> ContextWindow:
    """Read the source as written, truncating where the budget runs out.

    Segments are taken in document order rather than by relevance. Extraction
    reads the *whole* source; there is no query to be relevant to when the source
    fits, and reordering it would break the development history the order
    encodes.
    """
    selected: list[SelectedSegment] = []
    candidates: list[ContextCandidate] = []
    remaining = token_budget * CHARS_PER_TOKEN

    for segment in segments:
        length = len(segment.text)
        if remaining <= 0:
            candidates.append(
                ContextCandidate(
                    reference=segment.id,
                    disposition=ContextDisposition.EXCLUDED,
                    reason=f"beyond the {token_budget}-token context budget",
                )
            )
            continue
        if length <= remaining:
            selected.append(SelectedSegment(segment=segment, text=segment.text, truncated=False))
            candidates.append(
                ContextCandidate(
                    reference=segment.id,
                    disposition=ContextDisposition.SELECTED,
                    reason=f"fits the {token_budget}-token context budget",
                )
            )
            remaining -= length
            continue
        selected.append(
            SelectedSegment(segment=segment, text=segment.text[:remaining], truncated=True)
        )
        candidates.append(
            ContextCandidate(
                reference=segment.id,
                disposition=ContextDisposition.TRUNCATED,
                reason=(
                    f"cut to {remaining} of {length} characters by the "
                    f"{token_budget}-token context budget"
                ),
            )
        )
        remaining = 0

    return ContextWindow(
        selected=tuple(selected),
        candidates=tuple(candidates),
        token_budget=token_budget,
        strategy=ContextStrategy.IN_ORDER,
        ranked=False,
    )


# ----------------------------------------------------------------------
# Relevance ranked
# ----------------------------------------------------------------------


def _relevance_ranked(
    segments: Sequence[SourceSegment], *, query: str, token_budget: int
) -> ContextWindow:
    """Rank against ``query`` and fill the budget best-first — if ranking is needed.

    Whole segments only. The in-order strategy truncates because the prefix it
    keeps is continuous prose; a ranked fill has no such guarantee, and a segment
    cut to whatever space the previous three left over is a fragment nobody chose
    the length of.
    """
    budget_chars = token_budget * CHARS_PER_TOKEN
    if sum(len(segment.text) for segment in segments) <= budget_chars:
        return _whole_source(segments, token_budget=token_budget)

    scores = _bm25(segments, query)
    kept: set[str] = set()
    remaining = budget_chars
    # Ranked highest first, ties broken by document order: two segments the query
    # cannot tell apart should be taken in the order the author wrote them rather
    # than in whatever order the sort happened to leave them.
    for segment in _by_rank(segments, scores):
        if len(segment.text) <= remaining:
            kept.add(segment.id)
            remaining -= len(segment.text)

    ranking = _ranks(segments, scores)
    return ContextWindow(
        selected=tuple(
            SelectedSegment(segment=segment, text=segment.text, truncated=False)
            for segment in segments
            if segment.id in kept
        ),
        candidates=tuple(
            ContextCandidate(
                reference=segment.id,
                disposition=(
                    ContextDisposition.SELECTED
                    if segment.id in kept
                    else ContextDisposition.EXCLUDED
                ),
                reason=(
                    f"ranked {ranking[segment.id]} of {len(segments)} against the project's "
                    f"subject and {'fits' if segment.id in kept else 'does not fit'} the "
                    f"{token_budget}-token context budget"
                ),
                score=scores[segment.id],
            )
            for segment in segments
        ),
        token_budget=token_budget,
        strategy=ContextStrategy.RELEVANCE_RANKED,
        ranked=True,
    )


def _whole_source(segments: Sequence[SourceSegment], *, token_budget: int) -> ContextWindow:
    """Everything, unranked, because everything fits.

    No score is recorded. A zero would say the segment was scored and found
    worthless; the truth is that nothing was scored, and the reason says which.
    """
    return ContextWindow(
        selected=tuple(
            SelectedSegment(segment=segment, text=segment.text, truncated=False)
            for segment in segments
        ),
        candidates=tuple(
            ContextCandidate(
                reference=segment.id,
                disposition=ContextDisposition.SELECTED,
                reason=(
                    f"the whole source fits the {token_budget}-token context budget, "
                    "so nothing was ranked"
                ),
            )
            for segment in segments
        ),
        token_budget=token_budget,
        strategy=ContextStrategy.RELEVANCE_RANKED,
        ranked=False,
    )


def _bm25(segments: Sequence[SourceSegment], query: str) -> dict[str, float]:
    """BM25 relevance of each segment to ``query``.

    Chosen over raw term overlap because the two failure modes overlap costs are
    prone to both matter here: a long segment should not outrank a short one for
    mentioning the query in passing, and a word that appears in every segment
    should not be evidence of anything.
    """
    terms = _tokens(query)
    documents = {segment.id: _tokens(segment.text) for segment in segments}
    if not terms or not documents:
        return {segment.id: 0.0 for segment in segments}

    total = len(documents)
    average = sum(len(tokens) for tokens in documents.values()) / total
    frequencies = {
        term: sum(1 for tokens in documents.values() if term in tokens) for term in set(terms)
    }

    scores: dict[str, float] = {}
    for reference, tokens in documents.items():
        length = len(tokens) or 1
        score = 0.0
        for term in set(terms):
            occurrences = tokens.count(term)
            if not occurrences:
                continue
            frequency = frequencies[term]
            idf = math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
            score += idf * (
                occurrences
                * (BM25_K1 + 1)
                / (occurrences + BM25_K1 * (1 - BM25_B + BM25_B * length / average))
            )
        scores[reference] = round(score, 6)
    return scores


def _by_rank(segments: Sequence[SourceSegment], scores: dict[str, float]) -> list[SourceSegment]:
    """Strongest first, ties broken by document order."""
    return [
        segment
        for _, segment in sorted(
            enumerate(segments), key=lambda pair: (-scores[pair[1].id], pair[0])
        )
    ]


def _ranks(segments: Sequence[SourceSegment], scores: dict[str, float]) -> dict[str, int]:
    """Each segment's 1-based position in the ranking, for the recorded reason."""
    return {
        segment.id: position for position, segment in enumerate(_by_rank(segments, scores), start=1)
    }


def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN.finditer(text)]


__all__ = [
    "BM25_B",
    "BM25_K1",
    "CHARS_PER_TOKEN",
    "STRATEGY_VERSIONS",
    "ContextStrategy",
    "ContextWindow",
    "SelectedSegment",
    "select_context",
    "select_segments",
]
