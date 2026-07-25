"""Source ingestion (phase 06 §1).

Spec (plan/06 → Deliverables, *Source ingestion*; exit criterion "source ingested
immutably with segments, hashes, constraints, confidentiality flags"): import
Markdown / plain text / pasted notes, store an immutable ``SourceDocument`` with
parsed ``SourceSegment``s and content hashes, plus the project's constraints
(audience, platform, depth, confidential names, length, first-person allowed,
allowed providers, trace-retention consent) and its confidentiality /
provider-access flags.

Parsing is pinned in detail because everything downstream addresses the source
*by segment*: extraction records which segments it included and excluded, claims
cite the segments that support them, and a diff shows which segment an answer
changed. A segmenter that split a code fence at its blank line, or reported
offsets that no longer slice the original text, would corrupt every one of those
references while looking perfectly plausible.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArticleDepth, BranchStatus, SegmentKind, SourceFormat
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.stages.base import StageRunner
from groundscribe.stages.ingestion import IngestSource, parse_source
from groundscribe.storage.blob_store import content_hash
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowState
from stage_helpers import build_context

MARKDOWN = """\
# Caching the render pipeline

We shipped a read-through cache in March. Latency fell from 800ms to 120ms.

## What went wrong

The invalidation was wrong for a week.

```python
def key(request):
    return request.path

# no locale, no tenant
```

- keys ignored the locale
- the CDN cached the error page

> The postmortem itself is internal only.
"""

NOTES = """\
cache shipped march
p99 800 -> 120

invalidation broke for a week
"""


def constraints(**overrides: object) -> EditorialConstraints:
    """The project's constraints, with one field varied per test."""
    base = {
        "audience": "senior backend engineers",
        "platform": "personal blog",
        "depth": ArticleDepth.PRACTITIONER,
        "target_length_words": 1800,
        "first_person_allowed": True,
        "confidential_names": ("Northwind", "project-atlas"),
        "allowed_providers": ("ollama",),
        "trace_retention_consent": True,
    }
    return EditorialConstraints.model_validate(base | overrides)


def test_markdown_parses_into_kinds_with_offsets_that_slice_the_original() -> None:
    """Every segment names its kind and points back into the exact source text."""
    segments = parse_source(MARKDOWN, SourceFormat.MARKDOWN)

    assert [segment.kind for segment in segments] == [
        SegmentKind.HEADING,
        SegmentKind.PARAGRAPH,
        SegmentKind.HEADING,
        SegmentKind.PARAGRAPH,
        SegmentKind.CODE,
        SegmentKind.LIST,
        SegmentKind.QUOTE,
    ]
    assert [segment.ordinal for segment in segments] == list(range(7))
    for segment in segments:
        assert MARKDOWN[segment.char_start : segment.char_end] == segment.text
        assert segment.content_hash == content_hash(segment.text.encode("utf-8"))


def test_a_fenced_code_block_survives_the_blank_lines_inside_it() -> None:
    """A code fence is one segment: blank lines inside it are code, not separators."""
    code = next(
        segment
        for segment in parse_source(MARKDOWN, SourceFormat.MARKDOWN)
        if segment.kind is SegmentKind.CODE
    )

    assert code.text.startswith("```python")
    assert code.text.endswith("```")
    assert "no locale, no tenant" in code.text


def test_plain_text_and_pasted_notes_segment_on_blank_lines() -> None:
    """Unstructured input still segments, so claims can cite a passage of it."""
    for source_format in (SourceFormat.PLAIN_TEXT, SourceFormat.PASTED_NOTES):
        segments = parse_source(NOTES, source_format)
        assert [segment.kind for segment in segments] == [
            SegmentKind.PARAGRAPH,
            SegmentKind.PARAGRAPH,
        ]
        assert segments[1].text == "invalidation broke for a week"


async def test_ingestion_stores_the_document_its_segments_and_their_hashes(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The document is content-addressed and its segments are persisted with it."""
    context = build_context(db_session, snapshot_store)

    result = await StageRunner(context).run(
        IngestSource(
            title="Caching the render pipeline",
            text=MARKDOWN,
            source_format=SourceFormat.MARKDOWN,
            constraints=constraints(),
        )
    )
    ingested = result.value

    assert ingested.document.content_hash == content_hash(MARKDOWN.encode("utf-8"))
    assert ingested.document.media_type == "text/markdown"
    assert ingested.document.source_format is SourceFormat.MARKDOWN
    assert snapshot_store.read(ingested.snapshot).decode("utf-8") == MARKDOWN
    assert len(ingested.segments) == 7
    assert [segment.ordinal for segment in ingested.segments] == list(range(7))
    assert all(segment.document_id == ingested.document.id for segment in ingested.segments)
    stored = db_session.execute(
        select(domain_models.SourceSegment).where(
            domain_models.SourceSegment.document_id == ingested.document.id
        )
    ).scalars()
    assert len(list(stored)) == 7


async def test_ingestion_records_its_provenance_without_moving_the_run(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/00 → every artefact references a creating execution; §1 is pre-workflow."""
    context = build_context(db_session, snapshot_store)

    result = await StageRunner(context).run(
        IngestSource(title="Notes", text=NOTES, constraints=constraints())
    )
    ingested = result.value
    execution = result.execution

    assert execution is not None
    assert ingested.document.created_by_execution_id == execution.id
    assert ingested.snapshot.created_by_execution_id == execution.id
    assert ingested.constraints.created_by_execution_id == execution.id
    assert all(segment.created_by_execution_id == execution.id for segment in ingested.segments)
    assert [artifact.snapshot_id for artifact in execution.outputs] == [ingested.snapshot.id]
    assert context.engine.state is WorkflowState.SOURCE_INGESTED


async def test_re_ingesting_the_same_material_branches_instead_of_mutating(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/00 → no silent mutation: a re-ingest forks and supersedes its parent.

    The bytes deduplicate (identical content is one blob) while the *documents*
    stay distinct rows, so the corrected version is comparable to what it replaced
    rather than overwriting it.
    """
    context = build_context(db_session, snapshot_store)
    runner = StageRunner(context)

    first = (
        await runner.run(IngestSource(title="Notes", text=NOTES, constraints=constraints()))
    ).value
    revised = NOTES + "\nlocale key was missing\n"
    second = (
        await runner.run(
            IngestSource(
                title="Notes",
                text=revised,
                constraints=constraints(),
                parent=first.document,
            )
        )
    ).value

    assert second.document.id != first.document.id
    assert second.document.parent_id == first.document.id
    assert first.document.branch_status is BranchStatus.SUPERSEDED
    assert second.document.branch_status is BranchStatus.ACTIVE
    assert second.snapshot.parent_snapshot_id == first.snapshot.id
    # The parent's own content is untouched.
    assert snapshot_store.read(first.snapshot).decode("utf-8") == NOTES
    assert first.document.content_hash == content_hash(NOTES.encode("utf-8"))


async def test_identical_content_ingested_twice_shares_one_blob(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Content addressing: the same source stored twice costs one blob."""
    context = build_context(db_session, snapshot_store)
    runner = StageRunner(context)

    first = (
        await runner.run(IngestSource(title="Notes", text=NOTES, constraints=constraints()))
    ).value
    second = (
        await runner.run(IngestSource(title="Notes", text=NOTES, constraints=constraints()))
    ).value

    assert second.snapshot.id != first.snapshot.id
    assert second.snapshot.content_hash == first.snapshot.content_hash
    assert second.snapshot.content_location == first.snapshot.content_location


async def test_the_project_constraints_are_captured_in_full(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Every constraint plan/06 names is stored and reads back unchanged."""
    context = build_context(db_session, snapshot_store)
    declared = constraints()

    result = await StageRunner(context).run(
        IngestSource(title="Notes", text=NOTES, constraints=declared)
    )
    row = result.value.constraints

    assert row.project_id == context.project_id
    assert EditorialConstraints.model_validate(row) == declared


async def test_unchanged_constraints_are_reused_and_a_change_forks_a_version(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Constraints are versioned, not overwritten — and not duplicated for nothing.

    Re-declaring the same constraints must not litter the project with identical
    versions; changing one must not edit the version earlier artefacts were
    produced under.
    """
    context = build_context(db_session, snapshot_store)
    runner = StageRunner(context)

    first = (
        await runner.run(IngestSource(title="Notes", text=NOTES, constraints=constraints()))
    ).value
    again = (
        await runner.run(IngestSource(title="Notes 2", text=MARKDOWN, constraints=constraints()))
    ).value
    assert again.constraints.id == first.constraints.id

    changed = (
        await runner.run(
            IngestSource(
                title="Notes 3",
                text=NOTES,
                constraints=constraints(target_length_words=900),
            )
        )
    ).value

    assert changed.constraints.id != first.constraints.id
    assert changed.constraints.parent_id == first.constraints.id
    assert first.constraints.branch_status is BranchStatus.SUPERSEDED
    assert first.constraints.target_length_words == 1800


async def test_confidentiality_and_provider_access_are_recorded_flags(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 → confidentiality / provider-access flags; plan/13 enforces them.

    ``allowed_providers`` is an allow-list: a project that has not named a
    provider has not consented to it seeing the material, so the default answer is
    no rather than yes.
    """
    context = build_context(db_session, snapshot_store)

    result = await StageRunner(context).run(
        IngestSource(
            title="Internal postmortem",
            text=MARKDOWN,
            source_format=SourceFormat.MARKDOWN,
            constraints=constraints(),
            confidential=True,
        )
    )
    ingested = result.value

    assert ingested.document.confidential is True
    declared = EditorialConstraints.model_validate(ingested.constraints)
    assert declared.permits_provider("ollama") is True
    assert declared.permits_provider("anthropic") is False
    assert declared.confidential_names == ("Northwind", "project-atlas")
    assert declared.trace_retention_consent is True

    denied = constraints(allowed_providers=())
    assert denied.permits_provider("ollama") is False


def test_empty_source_material_is_refused() -> None:
    """A source with nothing in it is a mistake, not a document with no segments."""
    with pytest.raises(ValueError, match="no content"):
        parse_source("   \n\n  ", SourceFormat.MARKDOWN)
