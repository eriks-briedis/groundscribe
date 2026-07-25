"""Source ingestion: raw material in, addressable source document out (phase 06 §1).

plan/06 → *Source ingestion*: import Markdown / plain text / pasted notes, store
an immutable ``SourceDocument`` with parsed segments and content hashes, plus the
project's constraints and its confidentiality / provider-access flags.

Two properties matter more than the parsing itself.

**Everything downstream addresses the source by segment.** Extraction records the
segments it included and excluded, claims cite the segments that support them, and
an answer diff points at the segment it changed. So a segment carries its kind,
its character offsets into the document as ingested, and the hash of its own text:
a citation is then verifiable — slice the offsets out of the stored bytes and
compare — rather than merely plausible.

**No model is called here.** Ingestion is the one stage whose output is not
generated, which is why it takes no workflow edge (the run is already in
``SOURCE_INGESTED``) and why re-ingesting forks a document rather than editing one:
the correction has to stay comparable to what it replaced.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy import select

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType, BranchStatus, SegmentKind, SourceFormat
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.provenance import models
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.storage.blob_store import content_hash
from groundscribe.workflow.states import WorkflowAction

#: The media type recorded for each supported input format.
MEDIA_TYPES: dict[SourceFormat, str] = {
    SourceFormat.MARKDOWN: "text/markdown",
    SourceFormat.PLAIN_TEXT: "text/plain",
    SourceFormat.PASTED_NOTES: "text/plain",
}

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s)")
_QUOTE = re.compile(r"^\s*>")


@dataclass(frozen=True)
class ParsedSegment:
    """One passage of a source document, before it is persisted.

    A value object so parsing is testable without a database — the segmenter is
    the part most likely to be wrong, and the part every citation rests on.
    """

    ordinal: int
    kind: SegmentKind
    text: str
    char_start: int
    char_end: int

    @property
    def content_hash(self) -> str:
        """The content address of this segment's own text."""
        return content_hash(self.text.encode("utf-8"))


@dataclass(frozen=True)
class IngestedSource:
    """What ingestion produced: the document, its segments, and the constraints."""

    document: domain_models.SourceDocument
    segments: tuple[domain_models.SourceSegment, ...]
    snapshot: ArtifactSnapshot
    constraints: domain_models.ProjectConstraints


def parse_source(text: str, source_format: SourceFormat) -> tuple[ParsedSegment, ...]:
    """Split ``text`` into addressable segments, keeping exact offsets.

    Blank lines separate blocks, with one exception that matters: a fenced code
    block is a single segment however many blank lines it contains. Splitting a
    fence would produce segments that are not valid code, and an extraction quoting
    one of them would emit something that never appeared in the source.

    Plain text and pasted notes are segmented on blank lines only. Their lines have
    no structural meaning — a ``#`` in someone's notes is a note, not a heading —
    so inferring Markdown structure from them would invent structure the author
    did not write.
    """
    if not text.strip():
        raise ValueError("source material has no content to ingest")

    markdown = source_format is SourceFormat.MARKDOWN
    segments: list[ParsedSegment] = []
    for start, end in _block_spans(text, fenced=markdown):
        block = text[start:end]
        segments.append(
            ParsedSegment(
                ordinal=len(segments),
                kind=_classify(block) if markdown else SegmentKind.PARAGRAPH,
                text=block,
                char_start=start,
                char_end=end,
            )
        )
    return tuple(segments)


def _block_spans(text: str, *, fenced: bool) -> list[tuple[int, int]]:
    """Character spans of each block, trimmed of surrounding whitespace.

    Spans rather than substrings: the offsets are the product, and computing them
    from the text as it is walked is the only way they stay exact. Deriving them
    afterwards with ``str.find`` would silently point at the wrong copy of any
    passage that appears twice.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    offset = 0
    buffer = ""
    in_fence = False

    def flush() -> None:
        nonlocal buffer
        stripped = buffer.strip()
        if stripped:
            begin = start + (len(buffer) - len(buffer.lstrip()))
            spans.append((begin, begin + len(stripped)))
        buffer = ""

    for line in text.splitlines(keepends=True):
        if not buffer:
            start = offset
        offset += len(line)
        if fenced and _FENCE.match(line):
            # A fence both opens and closes a block: whatever precedes it is its
            # own segment, and the fenced body must not merge with what follows.
            if in_fence:
                buffer += line
                in_fence = False
                flush()
                continue
            flush()
            start = offset - len(line)
            in_fence = True
            buffer += line
            continue
        buffer += line
        if not in_fence and not line.strip():
            flush()
    flush()
    return spans


def _classify(block: str) -> SegmentKind:
    """What kind of Markdown block this is, judged by its first line."""
    first = block.splitlines()[0] if block.splitlines() else ""
    if _FENCE.match(first):
        return SegmentKind.CODE
    if _HEADING.match(first):
        return SegmentKind.HEADING
    if _QUOTE.match(first):
        return SegmentKind.QUOTE
    if _LIST_ITEM.match(first):
        return SegmentKind.LIST
    return SegmentKind.PARAGRAPH


class IngestSource:
    """Store one piece of source material, its segments and the project's constraints.

    Takes no workflow edge: the run is already in ``SOURCE_INGESTED`` by the time
    there is anything to ingest, and a stage that moved the machine here would have
    to invent a state for "before the source exists".
    """

    name: ClassVar[str] = "ingest_source"
    impl_version: ClassVar[str] = "1.0"
    entry_action: ClassVar[WorkflowAction | None] = None
    exit_action: ClassVar[WorkflowAction | None] = None

    def __init__(
        self,
        *,
        title: str,
        text: str,
        constraints: EditorialConstraints,
        source_format: SourceFormat = SourceFormat.PLAIN_TEXT,
        confidential: bool = False,
        uri: str | None = None,
        parent: domain_models.SourceDocument | None = None,
    ) -> None:
        self._title = title
        self._text = text
        self._constraints = constraints
        self._format = source_format
        self._confidential = confidential
        self._uri = uri
        self._parent = parent

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[IngestedSource]:
        """Parse, snapshot and persist the source; capture the constraints."""
        parsed = parse_source(self._text, self._format)
        snapshot = context.recorder.record_text_output(
            execution,
            artifact_type=ArtifactType.SOURCE_DOCUMENT,
            text=self._text,
            role="source_document",
            parent=self._parent.snapshot if self._parent is not None else None,
        )
        constraints = self._resolve_constraints(context, execution)
        document = self._store_document(context, execution, snapshot)
        segments = self._store_segments(context, execution, document, parsed)
        return StageResult(
            value=IngestedSource(
                document=document,
                segments=segments,
                snapshot=snapshot,
                constraints=constraints,
            ),
            outputs=(snapshot,),
            detail={
                "segments": len(segments),
                "source_format": self._format.value,
                "confidential": self._confidential,
            },
        )

    def _store_document(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        snapshot: ArtifactSnapshot,
    ) -> domain_models.SourceDocument:
        """Persist the document, superseding its parent rather than editing it."""
        if self._parent is not None:
            self._parent.branch_status = BranchStatus.SUPERSEDED
        document = domain_models.SourceDocument(
            id=uuid.uuid4().hex,
            project_id=context.project_id,
            title=self._title,
            media_type=MEDIA_TYPES[self._format],
            source_format=self._format,
            uri=self._uri,
            content_hash=snapshot.content_hash,
            snapshot_id=snapshot.id,
            confidential=self._confidential,
            created_by_execution_id=execution.id,
            parent_id=self._parent.id if self._parent is not None else None,
        )
        context.session.add(document)
        context.session.flush()
        return document

    def _store_segments(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        document: domain_models.SourceDocument,
        parsed: Sequence[ParsedSegment],
    ) -> tuple[domain_models.SourceSegment, ...]:
        """Persist the parsed segments against their document."""
        segments = tuple(
            domain_models.SourceSegment(
                id=f"{document.id}-{segment.ordinal}",
                document_id=document.id,
                ordinal=segment.ordinal,
                text=segment.text,
                kind=segment.kind,
                content_hash=segment.content_hash,
                char_start=segment.char_start,
                char_end=segment.char_end,
                created_by_execution_id=execution.id,
            )
            for segment in parsed
        )
        context.session.add_all(segments)
        context.session.flush()
        return segments

    def _resolve_constraints(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> domain_models.ProjectConstraints:
        """Reuse the constraints already in force, or version them if they changed.

        Comparing *values* is what makes this safe to call on every ingest: a
        project that re-declares the same constraints should not accumulate
        identical versions, and a project that changes one must not edit the
        version earlier artefacts were produced under.
        """
        active = self._active_constraints(context)
        if active is not None and EditorialConstraints.model_validate(active) == self._constraints:
            return active
        if active is not None:
            active.branch_status = BranchStatus.SUPERSEDED
        row = domain_models.ProjectConstraints(
            id=uuid.uuid4().hex,
            project_id=context.project_id,
            created_by_execution_id=execution.id,
            parent_id=active.id if active is not None else None,
            **_constraint_columns(self._constraints),
        )
        context.session.add(row)
        context.session.flush()
        return row

    def _active_constraints(
        self, context: PipelineContext
    ) -> domain_models.ProjectConstraints | None:
        """The project's live constraints version, if it has one."""
        stmt = select(domain_models.ProjectConstraints).where(
            domain_models.ProjectConstraints.project_id == context.project_id,
            domain_models.ProjectConstraints.branch_status == BranchStatus.ACTIVE,
        )
        return context.session.execute(stmt).scalars().first()


def _constraint_columns(constraints: EditorialConstraints) -> dict[str, object]:
    """The constraint value as column keyword arguments.

    The tuple-valued constraints become lists: JSON columns round-trip lists, and
    the schema re-validates them back into tuples on the way out.
    """
    data = constraints.model_dump()
    data["confidential_names"] = list(constraints.confidential_names)
    data["allowed_providers"] = list(constraints.allowed_providers)
    return data


__all__ = ["MEDIA_TYPES", "IngestSource", "IngestedSource", "ParsedSegment", "parse_source"]
