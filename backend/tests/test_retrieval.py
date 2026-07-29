"""Retrieval over source segments, and only when the source demands it (phase 12).

plan/12 → *Retrieval (conditional): full-text/embedding/hybrid source-segment
retrieval, added only when source collections exceed practical prompt limits;
once present, all candidate / selected / excluded segments are traceable via the
phase-03 context-selection record*, guarded by its named risk: *adding retrieval
"because it's common" — add only when source size demands it*.

Two claims are being pinned, and the second is the one that costs something.

**Ranking is conditional.** A source that fits the budget is sent whole, in the
order it was written, and the record says ranking never happened. Extraction
reads the *whole* source; when it can, relevance is not a question it has to ask,
and a strategy that ranked anyway would reorder a development history for no gain
and make every run look like a retrieval problem.

**Nothing is silently unseen.** When ranking does happen, every segment is in the
record with its score and what became of it. The alternative — recording only
what was sent — is the failure mode the whole provenance model exists to prevent:
an article that never mentions the incident in section four, and no way to tell
whether the model judged it irrelevant or never saw it.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from golden import relabel, with_segment_ids
from groundscribe.domain.enums import SegmentKind
from groundscribe.domain.models import SourceSegment
from groundscribe.provenance.enums import ContextDisposition
from groundscribe.stages.base import StageRunner
from groundscribe.stages.context import (
    CHARS_PER_TOKEN,
    ContextStrategy,
    ContextWindow,
    select_context,
)
from groundscribe.stages.extraction import ExtractSourceTruth
from groundscribe.storage.snapshot_store import SnapshotStore
from stage_helpers import scripted_context
from test_extraction import BUDGETED_MODEL, ingest_golden, script

#: What ``provenance_helpers.seed_project`` calls the project every stage test
#: runs under — and therefore the query extraction ranks its source against.
SUBJECT = "Caching write-up"

#: Three passages, one of which is about the query and two of which are not.
#: Short on purpose: the budgets below are in characters divided by
#: ``CHARS_PER_TOKEN``, and a fixture whose sizes have to be counted by hand is a
#: fixture whose test says something different from what it appears to say.
FIRST = "The team met on Tuesday."
SECOND = "Lunch came from downstairs."
THIRD = "Cache invalidation used the path."

QUERY = "cache invalidation"


def segment(ident: str, text: str, ordinal: int) -> SourceSegment:
    return SourceSegment(
        id=ident,
        document_id="d1",
        ordinal=ordinal,
        text=text,
        kind=SegmentKind.PARAGRAPH,
        content_hash="",
        char_start=0,
        char_end=len(text),
    )


@pytest.fixture
def segments() -> tuple[SourceSegment, ...]:
    return (segment("S0", FIRST, 0), segment("S1", SECOND, 1), segment("S2", THIRD, 2))


def budget_for(*texts: str) -> int:
    """A token budget that fits exactly ``texts`` and nothing more.

    Rounded up, because a budget that fell a character short of its own fixture
    would make every assertion below true for the wrong reason.
    """
    total = sum(len(text) for text in texts)
    return -(-total // CHARS_PER_TOKEN)


def disposition_of(window: ContextWindow, reference: str) -> ContextDisposition:
    return next(item.disposition for item in window.candidates if item.reference == reference)


# ----------------------------------------------------------------------
# Conditional: ranking happens only when the source does not fit
# ----------------------------------------------------------------------


def test_a_source_that_fits_is_never_ranked(segments: tuple[SourceSegment, ...]) -> None:
    """plan/12's risk, as a test: retrieval only when source size demands it.

    Everything is selected, in the order it was written, and no candidate carries
    a score — because none was computed. A score of zero would be a claim that the
    segment was judged irrelevant and kept anyway.
    """
    window = select_context(
        segments,
        strategy=ContextStrategy.RELEVANCE_RANKED,
        query=QUERY,
        token_budget=budget_for(FIRST, SECOND, THIRD) + 10,
    )

    assert [item.id for item in window.selected] == ["S0", "S1", "S2"]
    assert all(item.disposition is ContextDisposition.SELECTED for item in window.candidates)
    assert all(item.score is None for item in window.candidates)
    assert not window.ranked


def test_a_source_that_does_not_fit_is_selected_by_relevance_not_by_position(
    segments: tuple[SourceSegment, ...],
) -> None:
    """The point of the strategy: the last passage wins when it is the relevant one.

    In-order selection is asserted alongside it, because "relevance beat
    position" is only a claim about this strategy if position would otherwise
    have won — and it is what phase 06 does to the same input.
    """
    budget = budget_for(THIRD)

    ranked = select_context(
        segments, strategy=ContextStrategy.RELEVANCE_RANKED, query=QUERY, token_budget=budget
    )
    in_order = select_context(
        segments, strategy=ContextStrategy.IN_ORDER, query=QUERY, token_budget=budget
    )

    assert [item.id for item in ranked.selected] == ["S2"]
    assert disposition_of(ranked, "S0") is ContextDisposition.EXCLUDED
    assert [item.id for item in in_order.selected] == ["S0", "S1"]
    assert disposition_of(in_order, "S2") is ContextDisposition.EXCLUDED


def test_what_relevance_chose_is_still_sent_in_the_order_it_was_written(
    segments: tuple[SourceSegment, ...],
) -> None:
    """Ranking decides *what*; the document decides *how it reads*.

    The order of source material is itself information — it is the development
    history the extraction stage is asked to recover. Sending the passages
    strongest-first would hand the model a chronology that never happened.
    """
    window = select_context(
        segments,
        strategy=ContextStrategy.RELEVANCE_RANKED,
        query=QUERY,
        token_budget=budget_for(FIRST, THIRD),
    )

    assert [item.id for item in window.selected] == ["S0", "S2"]
    assert [item.reference for item in window.candidates] == ["S0", "S1", "S2"]


def test_every_candidate_carries_the_score_it_was_ranked_on(
    segments: tuple[SourceSegment, ...],
) -> None:
    """plan/12 → *candidate / selected / excluded segments + scores* are recorded.

    Including the ones that scored nothing. A segment missing from the record is
    indistinguishable from a segment the retrieval never saw, and the difference
    is the whole reason this record exists.
    """
    window = select_context(
        segments,
        strategy=ContextStrategy.RELEVANCE_RANKED,
        query=QUERY,
        token_budget=budget_for(THIRD),
    )

    scores = {item.reference: item.score for item in window.candidates}
    assert set(scores) == {"S0", "S1", "S2"}
    assert all(score is not None for score in scores.values())
    assert scores["S2"] is not None and scores["S0"] is not None
    assert scores["S2"] > scores["S0"]
    assert all(item.reason for item in window.candidates)


# ----------------------------------------------------------------------
# Traceability through the phase-03 record
# ----------------------------------------------------------------------


async def test_a_retrieved_run_records_every_segment_and_which_strategy_ran(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The provenance test plan/12 names, against a real stage execution.

    The strategy is recorded because two runs of the same stage over the same
    source can now legitimately differ, and a comparison that could not name the
    selection strategy would report the difference without its cause.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    # Scripted against what the retrieval actually chose, rather than against a
    # segment picked by hand: a budgeted run can only cite what it was shown, and
    # a fixture that guessed which passage that would be would be testing the
    # guess.
    chosen = select_context(
        source.segments,
        strategy=ContextStrategy.RELEVANCE_RANKED,
        query=SUBJECT,
        token_budget=60,
    ).selected[0]
    script(model_client, relabel(BUDGETED_MODEL, {"S1": chosen.id}))

    result = await StageRunner(context).run(
        ExtractSourceTruth(
            source=source,
            token_budget=60,
            context_strategy=ContextStrategy.RELEVANCE_RANKED,
        )
    )
    execution = result.execution

    assert execution is not None
    (selection,) = execution.context_selections
    assert selection.strategy == ContextStrategy.RELEVANCE_RANKED.value
    assert selection.strategy_version
    assert [item.reference for item in selection.items] == [s.id for s in source.segments]
    assert all(item.score is not None for item in selection.items)
    dispositions = {item.disposition for item in selection.items}
    assert ContextDisposition.SELECTED in dispositions
    assert ContextDisposition.EXCLUDED in dispositions

    sent = model_client.last_request
    assert sent is not None
    for item in selection.items:
        assert (item.reference in sent.prompt) is (item.disposition is ContextDisposition.SELECTED)


async def test_extraction_still_reads_the_source_in_order_unless_asked_otherwise(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Phase 06's behaviour is the default, and stays the default.

    Retrieval is a strategy a person or an experiment chooses, named in the fork
    vocabulary phase 12 already declared. Switching every run onto it because it
    exists is precisely what plan/12's risk warns against.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    script(model_client, with_segment_ids(BUDGETED_MODEL, source))

    result = await StageRunner(context).run(ExtractSourceTruth(source=source, token_budget=60))
    execution = result.execution

    assert execution is not None
    (selection,) = execution.context_selections
    assert selection.strategy == ContextStrategy.IN_ORDER.value
