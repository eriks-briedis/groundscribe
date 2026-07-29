"""Where a segment's confidentiality comes from (phase 13).

Spec (plan/13 → *Confidentiality flags* on source segments). The flags of the
previous test module are only worth having if something sets them, and the
person who knows which paragraph is sensitive is the author, at the moment they
paste the material in.

Two sources, and no third:

- **The document's own flag.** A source document imported as confidential
  produces confidential segments. Marking the document and then having to mark
  each of its paragraphs would be a checklist, and a checklist is a thing people
  half-finish.
- **The author's inline markers.** ``[[CONFIDENTIAL]] … [[/CONFIDENTIAL]]`` is
  already the convention phase 03's redactor honours on its way to storage. A
  segment containing one is ingested confidential, so the same marks that keep a
  passage out of the trace keep it out of the prompt and the article.

Reusing the redaction markers rather than inventing a second syntax is the point
of this module: two ways to say "this is sensitive" is two ways to say it in only
one of the places that matter.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from groundscribe.domain.confidentiality import Confidentiality, Exclusion
from groundscribe.domain.enums import ArticleDepth, SourceFormat
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.stages.base import StageRunner
from groundscribe.stages.ingestion import IngestSource, IngestedSource
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
async def test_a_confidential_document_makes_confidential_segments(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The document's flag reaches the spans everything downstream addresses.

    Extraction, context selection and validation all work in segments. A
    document-level boolean that stopped at the document would be a label on a
    row nothing consults.
    """
    ingested = await _ingest(db_session, snapshot_store, PUBLIC, confidential=True)

    assert ingested.document.confidential
    assert all(
        segment.confidentiality is Confidentiality.CONFIDENTIAL for segment in ingested.segments
    )
    assert all(not segment.flags.may_be_published for segment in ingested.segments)


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
    """One mark, three refusals — the same set the classification implies."""
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
    """
    context = build_context(db_session, snapshot_store, state=WorkflowState.SOURCE_INGESTED)
    stage = IngestSource(
        title="Cache postmortem",
        text=MARKED,
        constraints=CONSTRAINTS,
        source_format=SourceFormat.MARKDOWN,
    )
    result = await StageRunner(context).run(stage)

    assert result.detail["confidential_segments"] == 1
