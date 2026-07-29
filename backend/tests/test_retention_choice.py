"""A project's retention mode is a declared choice, not a deployment default (phase 13).

Spec (plan/13 → *Trace-retention modes … local-first may default to detailed
retention but the choice is explicit*).

The modes themselves are pinned in ``test_retention_modes``. What is pinned here
is that a project *has* one, that it is stored beside the rest of its constraints
rather than in a config file somewhere, and that resuming a run puts it in force
before anything is recorded.

Storing it with the constraints is the decision. Constraints are versioned and
branch rather than being edited (phase 06), so "what was this project's retention
mode when that run was recorded?" stays answerable — which is exactly the
question someone asks when a trace turns out to be thinner than they expected.

Under ``redacted_full`` the recorder is also handed the project's restricted
source material, so the mode is not merely stored but connected to the material
it is about.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from groundscribe.app.services import resume_run
from groundscribe.domain import models as domain_models
from groundscribe.domain.confidentiality import Confidentiality
from groundscribe.domain.enums import SegmentKind
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.privacy.retention import RetentionMode
from groundscribe.provenance.enums import InvocationOutcome
from groundscribe.provenance.schemas import EffectiveRequest
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowState
from provenance_helpers import seed_project
from service_helpers import build_harness
from stage_helpers import DEFAULT_CONSTRAINTS

SECRET = "Northwind threatened to terminate the contract over the outage in March."


def test_a_project_that_says_nothing_keeps_everything() -> None:
    """The default is the mode that loses least. A trace cannot be un-thinned."""
    assert DEFAULT_CONSTRAINTS.trace_retention_mode is RetentionMode.FULL
    assert (
        EditorialConstraints(
            audience="engineers", platform="blog", depth=DEFAULT_CONSTRAINTS.depth
        ).trace_retention_mode
        is RetentionMode.FULL
    )


def _resume(session: Session, snapshots: SnapshotStore, mode: RetentionMode) -> object:
    """A run resumed for a project that declared ``mode``."""
    harness = build_harness(session, snapshots)
    project_id = seed_project(session)
    session.add(
        domain_models.ProjectConstraints(
            id="constraints-retention",
            project_id=project_id,
            audience=DEFAULT_CONSTRAINTS.audience,
            platform=DEFAULT_CONSTRAINTS.platform,
            depth=DEFAULT_CONSTRAINTS.depth,
            allowed_providers=list(DEFAULT_CONSTRAINTS.allowed_providers),
            trace_retention_mode=mode,
        )
    )
    document = domain_models.SourceDocument(
        id="doc-retention", project_id=project_id, title="Postmortem"
    )
    session.add(document)
    session.flush()
    session.add(
        domain_models.SourceSegment(
            id="seg-retention",
            document_id=document.id,
            ordinal=0,
            text=SECRET,
            kind=SegmentKind.PARAGRAPH,
            confidentiality=Confidentiality.CONFIDENTIAL,
        )
    )
    session.flush()

    run = harness.runtime.recorder.start_run(project_id=project_id)
    harness.runtime.positions.open(run, state=WorkflowState.SOURCE_INGESTED)
    session.flush()
    return resume_run(harness.runtime, run)


def test_the_declared_mode_survives_a_round_trip(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Stored with the constraints, read back with them."""
    resumed = _resume(db_session, snapshot_store, RetentionMode.NO_RAW_PROVIDER_PAYLOADS)

    assert (
        resumed.context.constraints.trace_retention_mode  # type: ignore[attr-defined]
        is RetentionMode.NO_RAW_PROVIDER_PAYLOADS
    )


@pytest.mark.asyncio
async def test_resuming_a_run_puts_the_project_s_mode_in_force(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The mode is applied before anything is recorded, not after.

    A recorder that adopted the project's choice only on the *next* command
    would write the first call of every run under the deployment default, which
    is the one call most likely to carry the prompt someone was trying not to
    keep.
    """
    resumed = _resume(db_session, snapshot_store, RetentionMode.METADATA_AND_STRUCTURED_ONLY)
    recorder = resumed.context.recorder  # type: ignore[attr-defined]
    execution = recorder.start_stage(resumed.run, stage="extract_source_truth")  # type: ignore[attr-defined]

    invocation = recorder.record_model_invocation(
        execution,
        request=EffectiveRequest(
            template_id="extract_source_truth", template_version="1", rendered_prompt=SECRET
        ),
        provider="ollama",
        model="llama3.1:70b-instruct",
        outcome=InvocationOutcome.ACCEPTED,
        raw_response="{}",
        parsed_response={"summary": "s"},
    )

    assert invocation.retention_mode is RetentionMode.METADATA_AND_STRUCTURED_ONLY
    assert invocation.request_snapshot is None
    assert invocation.raw_response_snapshot is None


@pytest.mark.asyncio
async def test_redacted_full_is_handed_the_material_it_is_about(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The mode names a policy; the project's flagged source is what it applies to.

    Wiring these together in ``resume_run`` is what stops ``redacted_full`` being
    a setting that reads well and removes nothing.
    """
    resumed = _resume(db_session, snapshot_store, RetentionMode.REDACTED_FULL)
    recorder = resumed.context.recorder  # type: ignore[attr-defined]
    execution = recorder.start_stage(resumed.run, stage="extract_source_truth")  # type: ignore[attr-defined]

    invocation = recorder.record_model_invocation(
        execution,
        request=EffectiveRequest(
            template_id="extract_source_truth",
            template_version="1",
            rendered_prompt=f"Consider: {SECRET}",
        ),
        provider="ollama",
        model="llama3.1:70b-instruct",
        outcome=InvocationOutcome.ACCEPTED,
    )

    assert invocation.request_snapshot is not None
    stored = snapshot_store.read(invocation.request_snapshot).decode("utf-8")
    assert SECRET not in stored
    assert "REDACTED" in stored
