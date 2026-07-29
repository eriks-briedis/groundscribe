"""The export guard sees the project's flagged material (phase 13).

Spec (plan/13 → *Confidentiality flags … enforced at final validation and at
export*). Validation is the gate that gives a person something to act on; this is
the gate that is not allowed to be forgotten. It sits on the ``approve_final``
transition itself, so every path to publication passes it — including paths added
after this phase, by someone who has not read it.

The engine has had the guard since phase 05, but it only knew the project's
declared confidential *names*. What it did not know was the material a person
flagged on the source. That is what these tests wire in, and one of them asserts
the wiring rather than the guard, because a guard nobody hands the evidence to
passes everything.

The second property is smaller and easy to lose: the refusal must not quote the
material it refused. An error message is logged, shown, and sometimes pasted into
a bug report — three more places for a leak that started as a safeguard.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from groundscribe.app.services import Resumed, resume_run
from groundscribe.domain import models as domain_models
from groundscribe.domain.confidentiality import Confidentiality
from groundscribe.domain.enums import ArtifactType, SegmentKind
from groundscribe.provenance.enums import ActorType
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.errors import ConfidentialMaterialError
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from provenance_helpers import seed_project
from service_helpers import build_harness
from stage_helpers import DEFAULT_CONSTRAINTS

SECRET = "Northwind threatened to terminate the contract over the outage in March."


def _flag_material(session: Session, project_id: str) -> None:
    """Give the project one confidential paragraph."""
    document = domain_models.SourceDocument(
        id="doc-export", project_id=project_id, title="Postmortem"
    )
    session.add(document)
    session.flush()
    session.add(
        domain_models.SourceSegment(
            id="seg-export",
            document_id=document.id,
            ordinal=0,
            text=SECRET,
            kind=SegmentKind.PARAGRAPH,
            confidentiality=Confidentiality.CONFIDENTIAL,
        )
    )
    session.flush()


async def _run_at_approval(
    session: Session, snapshots: SnapshotStore, body: bytes
) -> tuple[Resumed, domain_models.ArtifactSnapshot]:
    """A run parked at human approval, over a project with one flagged paragraph."""
    harness = build_harness(session, snapshots)
    project_id = seed_project(session)
    session.add(
        domain_models.ProjectConstraints(
            id="constraints-export",
            project_id=project_id,
            audience=DEFAULT_CONSTRAINTS.audience,
            platform=DEFAULT_CONSTRAINTS.platform,
            depth=DEFAULT_CONSTRAINTS.depth,
            allowed_providers=list(DEFAULT_CONSTRAINTS.allowed_providers),
        )
    )
    session.flush()
    _flag_material(session, project_id)
    run = harness.runtime.recorder.start_run(project_id=project_id)
    harness.runtime.positions.open(run, state=WorkflowState.FINAL_VALIDATING)
    session.flush()

    resumed = resume_run(harness.runtime, run)
    version = snapshots.write(
        artifact_type=ArtifactType.ARTICLE_VERSION,
        content=body,
        created_by_execution_id=resumed.engine.execution.id,
    )
    resumed.engine.apply(WorkflowAction.VALIDATION_PASSED, artifacts=(version,))
    return resumed, version


@pytest.mark.asyncio
async def test_approving_an_article_that_reprints_flagged_material_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The transition itself refuses, whatever route asked for it."""
    resumed, version = await _run_at_approval(
        db_session, snapshot_store, f'{{"body":"{SECRET}"}}'.encode()
    )

    with pytest.raises(ConfidentialMaterialError):
        resumed.engine.apply(
            WorkflowAction.APPROVE_FINAL,
            actor_id="ada",
            actor_type=ActorType.USER,
            artifacts=(version,),
        )


@pytest.mark.asyncio
async def test_the_refusal_does_not_quote_what_it_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """An error message is logged, shown, and pasted into bug reports.

    Naming the version is enough to act on. Repeating the sentence would turn a
    safeguard into another copy of the thing it was guarding.
    """
    resumed, version = await _run_at_approval(
        db_session, snapshot_store, f'{{"body":"{SECRET}"}}'.encode()
    )

    with pytest.raises(ConfidentialMaterialError) as raised:
        resumed.engine.apply(
            WorkflowAction.APPROVE_FINAL,
            actor_id="ada",
            actor_type=ActorType.USER,
            artifacts=(version,),
        )

    assert SECRET not in str(raised.value)
    assert version.id in str(raised.value)


@pytest.mark.asyncio
async def test_an_ordinary_article_still_publishes(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A project with flagged material publishes ordinary articles as before."""
    resumed, version = await _run_at_approval(
        db_session, snapshot_store, b'{"body":"a caching write-up"}'
    )

    resumed.engine.apply(
        WorkflowAction.APPROVE_FINAL,
        actor_id="ada",
        actor_type=ActorType.USER,
        artifacts=(version,),
    )

    assert resumed.engine.state is WorkflowState.COMPLETED
