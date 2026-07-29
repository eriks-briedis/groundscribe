"""Material flagged excluded-from-model-input never reaches a provider (phase 13).

Spec (plan/13 → *Confidentiality-aware request construction*; test-first
specification, *Excluded-from-input never sent*): flagged-excluded material is
absent from the effective request sent to any provider.

The check belongs in context selection, which is the one place every stage passes
source material through on its way to a prompt. Putting it in each stage instead
would make the guarantee a property of how many stages remembered it.

Three things are asserted separately because they can fail separately:

1. The segment is **withheld** — it is not among the selected text.
2. The withholding is **traceable** — the context-selection record lists it as
   excluded, with a reason that names confidentiality rather than the budget.
   Material that vanishes without a record is indistinguishable from material
   that was never there, which is exactly the confusion plan/03 exists to
   prevent.
3. Withholding **does not cost budget**. A confidential paragraph that still
   reserved its own space would silently push publishable material out of the
   prompt.

The last test goes all the way to the wire: whatever the selection record says,
the assertion that matters is that the bytes never left.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from golden import with_segment_ids
from groundscribe.domain import models as domain_models
from groundscribe.domain.confidentiality import Confidentiality, Exclusion
from groundscribe.domain.enums import SegmentKind, SourceFormat
from groundscribe.provenance.enums import ContextDisposition
from groundscribe.stages.base import StageRunner
from groundscribe.stages.context import ContextStrategy, select_context
from groundscribe.stages.extraction import EXTRACTION_STAGE, ExtractSourceTruth
from groundscribe.stages.ingestion import IngestSource
from groundscribe.storage.snapshot_store import SnapshotStore
from stage_helpers import DEFAULT_CONSTRAINTS, scripted_context

SECRET = "Northwind threatened to leave over the outage."

SOURCE = f"""\
We shipped a read-through cache in March.

[[CONFIDENTIAL]]
{SECRET}
[[/CONFIDENTIAL]]

p99 latency fell from 810ms to 120ms.
"""

#: What an honest model returns having been shown the two publishable passages
#: and nothing else — one claim, citing a passage that was actually offered.
SCRIPTED_MODEL: dict[str, object] = {
    "schema_version": 1,
    "summary": "A read-through cache cut p99 render latency.",
    "claims": [
        {
            "id": "c1",
            "text": "p99 latency fell from 810ms to 120ms.",
            "classification": "directly_supported_fact",
            "evidence": [{"segment_ids": ["S2"], "quote": "p99 latency fell from 810ms to 120ms"}],
            "qualification_required": False,
        }
    ],
}


def _segment(
    ordinal: int,
    text: str,
    *,
    confidentiality: Confidentiality = Confidentiality.PUBLISHABLE,
    excluded: tuple[Exclusion, ...] = (),
) -> domain_models.SourceSegment:
    """A detached segment; selection reads the fields, never the session."""
    return domain_models.SourceSegment(
        id=f"seg-{ordinal}",
        document_id="doc-1",
        ordinal=ordinal,
        text=text,
        kind=SegmentKind.PARAGRAPH,
        confidentiality=confidentiality,
        excluded=list(excluded),
    )


@pytest.mark.parametrize("strategy", list(ContextStrategy))
def test_a_confidential_segment_is_never_selected(strategy: ContextStrategy) -> None:
    """Whichever strategy is running, the flagged span is not offered."""
    segments = [
        _segment(0, "We shipped a read-through cache in March."),
        _segment(1, SECRET, confidentiality=Confidentiality.CONFIDENTIAL),
        _segment(2, "p99 latency fell from 810ms to 120ms."),
    ]

    window = select_context(
        segments, strategy=strategy, query="read-through cache latency", token_budget=1000
    )

    assert [item.id for item in window.selected] == ["seg-0", "seg-2"]
    assert all(SECRET not in item.text for item in window.selected)


def test_an_explicit_input_exclusion_is_enough_on_its_own() -> None:
    """Publishable material can still be withheld from the model.

    The classification is not the only way in: a person may want a paragraph in
    the article that they do not want a hosted model reasoning over.
    """
    segments = [
        _segment(0, "public"),
        _segment(1, "withheld", excluded=(Exclusion.MODEL_INPUT,)),
    ]

    window = select_context(segments, token_budget=1000)

    assert [item.id for item in window.selected] == ["seg-0"]


@pytest.mark.parametrize("strategy", list(ContextStrategy))
def test_the_record_says_it_was_confidentiality_and_not_the_budget(
    strategy: ContextStrategy,
) -> None:
    """Every segment is accounted for, and the reason distinguishes the two causes.

    A reader of the trace who cannot tell "withheld because it is confidential"
    from "did not fit" cannot tell a working safeguard from a small budget.
    """
    segments = [
        _segment(0, "We shipped a read-through cache in March."),
        _segment(1, SECRET, confidentiality=Confidentiality.CONFIDENTIAL),
    ]

    window = select_context(segments, strategy=strategy, query="cache", token_budget=1000)

    assert [candidate.reference for candidate in window.candidates] == ["seg-0", "seg-1"]
    withheld = next(item for item in window.candidates if item.reference == "seg-1")
    assert withheld.disposition is ContextDisposition.EXCLUDED
    assert "confidential" in withheld.reason
    assert "budget" not in withheld.reason


def test_withheld_material_does_not_spend_the_budget() -> None:
    """A confidential paragraph must not push publishable material out of the prompt.

    Without this, marking a passage confidential would quietly degrade the
    article: the budget would be spent on something the model never sees.
    """
    long_secret = "x" * 400
    segments = [
        _segment(0, "a" * 40, confidentiality=Confidentiality.CONFIDENTIAL),
        _segment(1, long_secret, confidentiality=Confidentiality.CONFIDENTIAL),
        _segment(2, "b" * 40),
    ]

    window = select_context(segments, token_budget=20)

    kept = next(item for item in window.candidates if item.reference == "seg-2")
    assert kept.disposition is ContextDisposition.SELECTED
    assert [item.id for item in window.selected] == ["seg-2"]


async def test_the_flagged_text_never_crosses_the_wire(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The assertion that actually matters: the bytes did not leave.

    Run end to end through extraction so the whole chain is exercised — parse,
    flag, select, render, send — rather than the selection function alone.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    ingested = (
        await StageRunner(context).run(
            IngestSource(
                title="Cache postmortem",
                text=SOURCE,
                source_format=SourceFormat.MARKDOWN,
                constraints=DEFAULT_CONSTRAINTS,
            )
        )
    ).value
    model_client.script_response(EXTRACTION_STAGE, with_segment_ids(SCRIPTED_MODEL, ingested))

    result = await StageRunner(context).run(ExtractSourceTruth(source=ingested))

    sent = model_client.last_request
    assert sent is not None
    assert SECRET not in sent.prompt
    assert all(SECRET not in message.content for message in sent.messages)

    execution = result.execution
    assert execution is not None
    (selection,) = execution.context_selections
    withheld = [item for item in selection.items if item.disposition is ContextDisposition.EXCLUDED]
    assert len(withheld) == 1
    assert "confidential" in withheld[0].reason


async def test_the_persisted_request_does_not_hold_it_either(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Never sent means never stored: the recorded request is the sent one.

    Phase 03 redacts on the way to storage, which would catch the marker even if
    selection had let it through. This asserts the stronger property — that the
    material was withheld *before* the call, so redaction has nothing to do.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    ingested = (
        await StageRunner(context).run(
            IngestSource(
                title="Cache postmortem",
                text=SOURCE,
                source_format=SourceFormat.MARKDOWN,
                constraints=DEFAULT_CONSTRAINTS,
            )
        )
    ).value
    model_client.script_response(EXTRACTION_STAGE, with_segment_ids(SCRIPTED_MODEL, ingested))

    result = await StageRunner(context).run(ExtractSourceTruth(source=ingested))
    execution = result.execution
    assert execution is not None

    (invocation,) = execution.model_invocations
    assert invocation.request_snapshot is not None
    stored = snapshot_store.read(invocation.request_snapshot).decode("utf-8")
    assert SECRET not in stored
    assert "REDACTED" not in stored
