"""Where a segment's confidentiality comes from (phase 13).

Spec (plan/13 → *Confidentiality flags* on source segments). The flags of the
previous test module are only worth having if something sets them, and the
person who knows which paragraph is sensitive is the author, at the moment they
paste the material in.

Two sources, and no third. They say deliberately different things, and the gap
between them is what these tests are mostly about:

- **The author's inline markers** are the strong statement.
  ``[[CONFIDENTIAL]] … [[/CONFIDENTIAL]]`` already means "this must not leave the
  machine" everywhere else in the system — phase 03's redactor deletes the span
  before storage, and ``AnswerResponse.CONFIDENTIAL`` uses those words for the
  same mark. So a marked segment is confidential: barred from the prompt, the
  article and any exported trace.
- **The document's own import flag** is the weaker one. It makes every segment
  *internal* — reasoned over locally, never published — plus an explicit
  exclusion from exported traces. Marking a whole postmortem sensitive is a
  request not to publish it, not a request for an article that can never be
  written.

Reusing the redaction markers rather than inventing a second syntax is the other
point of this module: two ways to say "this is sensitive" is two ways to say it
in only one of the places that matter.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from groundscribe.domain.confidentiality import Confidentiality, Exclusion
from groundscribe.domain.enums import ArticleDepth, SourceFormat
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.stages.base import StageRunner
from groundscribe.stages.ingestion import IngestedSource, IngestSource
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowState
from stage_helpers import build_context

CONSTRAINTS = EditorialConstraints(
    audience="platform engineers",
    platform="blog",
    depth=ArticleDepth.PRACTITIONER,
)

PUBLIC = """\
We shipped a read-through cache in March.

Latency fell from 800ms to 120ms.
"""

MARKED = """\
We shipped a read-through cache in March.

[[CONFIDENTIAL]]
Northwind threatened to leave over the outage.
[[/CONFIDENTIAL]]

Latency fell from 800ms to 120ms.
"""


async def _ingest(
    db_session: Session,
    snapshot_store: SnapshotStore,
    text: str,
    *,
    confidential: bool = False,
) -> IngestedSource:
    context = build_context(db_session, snapshot_store, state=WorkflowState.SOURCE_INGESTED)
    stage = IngestSource(
        title="Cache postmortem",
        text=text,
        constraints=CONSTRAINTS,
        source_format=SourceFormat.MARKDOWN,
        confidential=confidential,
    )
    return (await StageRunner(context).run(stage)).value


@pytest.mark.asyncio
async def test_ordinary_material_is_publishable(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Nothing is flagged unless something said so."""
    ingested = await _ingest(db_session, snapshot_store, PUBLIC)

    assert [segment.confidentiality for segment in ingested.segments] == [
        Confidentiality.PUBLISHABLE,
        Confidentiality.PUBLISHABLE,
    ]
    assert all(segment.flags.may_be_sent_to_a_provider for segment in ingested.segments)


@pytest.mark.asyncio
async def test_a_confidential_document_makes_internal_segments(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The document's flag reaches the spans everything downstream addresses.

    *Internal*, not confidential, and the difference is the whole point of
    importing a document this way: a person marking a whole postmortem sensitive
    is asking for it not to be published, not asking for an article they can
    never write. Barring the model from the entire source would make the import
    flag mean "ingest this and then do nothing with it".

    The passage they actually cannot let out is the one they mark inline, and
    that is the next test.
    """
    ingested = await _ingest(db_session, snapshot_store, PUBLIC, confidential=True)

    assert ingested.document.confidential
    assert all(segment.confidentiality is Confidentiality.INTERNAL for segment in ingested.segments)
    assert all(segment.flags.may_be_sent_to_a_provider for segment in ingested.segments)
    assert all(not segment.flags.may_be_published for segment in ingested.segments)


@pytest.mark.asyncio
async def test_a_confidential_document_stays_out_of_exported_traces(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Internal *plus* the trace boundary, named explicitly.

    An exported trace is a shared artefact — plan/13 has it going to a debugging
    thread or a portfolio — so material a person called confidential should not
    ride out inside one. The classification alone does not say that, so the
    import writes the exclusion down rather than widening what *internal* means
    for everyone else.
    """
    ingested = await _ingest(db_session, snapshot_store, PUBLIC, confidential=True)

    assert all(not segment.flags.may_be_exported_in_traces for segment in ingested.segments)
    assert all(Exclusion.EXPORTED_TRACES in segment.flags.excluded for segment in ingested.segments)


@pytest.mark.asyncio
async def test_an_inline_marker_flags_only_the_segment_it_is_in(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The author marks a passage; the rest of the document is unaffected.

    This is the case a document-level flag cannot express, and the reason the
    flags live on segments: one sensitive paragraph should not make the whole
    postmortem unusable.
    """
    ingested = await _ingest(db_session, snapshot_store, MARKED)

    marked = [segment for segment in ingested.segments if "Northwind" in segment.text]
    assert len(marked) == 1
    assert marked[0].confidentiality is Confidentiality.CONFIDENTIAL

    rest = [segment for segment in ingested.segments if "Northwind" not in segment.text]
    assert rest
    assert all(segment.confidentiality is Confidentiality.PUBLISHABLE for segment in rest)


@pytest.mark.asyncio
async def test_the_marked_segment_is_barred_from_all_three_boundaries(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """One mark, three refusals — the same set the classification implies.

    The inline marker is the strong statement, and it is strong because phase 03
    already reads it that way: the redactor deletes the span on its way to
    storage, and ``AnswerResponse.CONFIDENTIAL`` describes the same mark as
    material that "must not leave the machine". A marker that kept a passage out
    of the trace but put it in the prompt would mean two different things in two
    places.
    """
    ingested = await _ingest(db_session, snapshot_store, MARKED)
    marked = next(segment for segment in ingested.segments if "Northwind" in segment.text)

    assert marked.flags.exclusions == frozenset(Exclusion)


@pytest.mark.asyncio
async def test_the_stage_reports_what_it_flagged(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The count lands in the execution's detail, where a person can see it.

    Ingestion is where sensitivity enters the system. A run that quietly flagged
    three paragraphs and one that flagged none should not look identical in the
    trace.

    *Restricted* rather than *confidential*, because the two classifications that
    are not publishable both matter here: internal material is withheld from the
    article just as surely as confidential material is, and a count that named
    only one of them would under-report exactly the imports that flagged the most.
    """
    context = build_context(db_session, snapshot_store, state=WorkflowState.SOURCE_INGESTED)
    stage = IngestSource(
        title="Cache postmortem",
        text=MARKED,
        constraints=CONSTRAINTS,
        source_format=SourceFormat.MARKDOWN,
    )
    result = await StageRunner(context).run(stage)

    assert result.detail["restricted_segments"] == 1
