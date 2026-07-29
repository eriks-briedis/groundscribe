"""Exporting and deleting one project's trace (phase 13).

plan/13 → *project-level trace export + deletion; sanitised trace export with
warnings before exporting confidential material; sanitised execution report*.

**The warning comes before the artefact.** An export that produced confidential
material and mentioned it afterwards has already produced it: by the time anyone
reads the warning the bytes are on disk, in a chat window, or attached to a
ticket. So a full export of a project holding confidential material raises, and
the only way past it is an argument at the call site — where a reviewer reads it,
rather than in a setting somewhere else. The sanitised path needs no
acknowledgement, which is what keeps the acknowledgement from becoming a box
people tick without reading.

**Sanitising removes content and keeps the record.** The run, the stage, the
model, the outcome, the cost all survive; the payload text does not, and its
absence is marked rather than silent. A reader must be able to tell "there was no
prompt" from "the prompt was withheld", and an omission cannot say which.

**Deletion is of content, not of history.** Trace events are append-only by
construction — phase 03 rejects updates and deletes at the mapper — so "delete my
traces" cannot mean "make it look like nothing ran", and it should not: the
record that a call happened is what makes every cost and repair-rate number
computed from it true. Deletion drops the stored payloads, and it stops at
anything another project still references, because content addressing means two
projects that sent the same request share one blob.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.privacy.material import restricted_spans
from groundscribe.provenance import models
from groundscribe.storage.snapshot_store import SnapshotStore

#: What a sanitised export puts where a payload was.
#:
#: A marker rather than an omitted key: absence and withholding look identical to
#: a reader, and only one of them is honest about what the document is.
WITHHELD = "[WITHHELD:sanitised-export]"


class ConfidentialExportRefused(Exception):
    """A full export would carry material the project holds confidential.

    Deliberately says how much and not what. An export refusal that printed the
    material would be its own leak, in a message that ends up in logs and bug
    reports.
    """


@dataclass(frozen=True)
class TraceExport:
    """One project's execution records, ready to be written out."""

    project_id: str
    sanitised: bool
    runs: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()
    withheld_payloads: int = 0

    def to_json(self) -> str:
        """The export as a document, stable enough to diff between runs."""
        return json.dumps(
            {
                "project_id": self.project_id,
                "sanitised": self.sanitised,
                "warnings": list(self.warnings),
                "withheld_payloads": self.withheld_payloads,
                "runs": list(self.runs),
            },
            indent=2,
            sort_keys=True,
        )


@dataclass(frozen=True)
class TraceDeletion:
    """What a deletion removed, so an irreversible act can be checked."""

    project_id: str
    payloads: int = 0
    bytes_reclaimed: int = 0
    records_kept: int = 0
    #: Payloads left alone because another project still references the same
    #: content. Reported rather than hidden: a person who asked for deletion is
    #: owed the count of what survived and why.
    shared_payloads: int = field(default=0)


def export_traces(
    session: Session,
    snapshots: SnapshotStore,
    project_id: str,
    *,
    sanitise: bool = False,
    confidential_material_acknowledged: bool = False,
) -> TraceExport:
    """Everything recorded for ``project_id``, in one document.

    Raises :class:`ConfidentialExportRefused` when a *full* export would carry
    material the project holds confidential and the caller has not said it knows.
    """
    restricted = restricted_spans(session, project_id)
    warnings: list[str] = []
    if restricted and not sanitise:
        if not confidential_material_acknowledged:
            raise ConfidentialExportRefused(
                f"project {project_id} holds {len(restricted)} passage(s) of confidential "
                "source material; a full trace export may carry them out of this machine. "
                "Export with sanitise=True, or pass confidential_material_acknowledged=True "
                "to say that you intend to."
            )
        warnings.append(
            f"this export may contain {len(restricted)} passage(s) of confidential source material"
        )

    withheld = _Counter()
    runs = tuple(
        _run(session, snapshots, run, sanitise=sanitise, withheld=withheld)
        for run in session.scalars(
            select(models.PipelineRun)
            .where(models.PipelineRun.project_id == project_id)
            .order_by(models.PipelineRun.started_at, models.PipelineRun.id)
        ).all()
    )
    return TraceExport(
        project_id=project_id,
        sanitised=sanitise,
        runs=runs,
        warnings=tuple(warnings),
        withheld_payloads=withheld.total,
    )


def delete_traces(session: Session, snapshots: SnapshotStore, project_id: str) -> TraceDeletion:
    """Drop this project's stored payloads, keeping the records of what ran.

    The snapshot rows go and the references are cleared; a *blob* is left in
    place, because it is content-addressed and another project's snapshot may
    resolve to the same bytes. Reclaiming the file would then delete data nobody
    asked to delete, and nothing would report it.
    """
    payloads = 0
    reclaimed = 0
    shared = 0
    records = 0

    for invocation in _invocations(session, project_id):
        records += 1
        for attribute in (
            "request_snapshot",
            "raw_response_snapshot",
            "parsed_response_snapshot",
            "validated_response_snapshot",
        ):
            snapshot = getattr(invocation, attribute)
            if snapshot is None:
                continue
            setattr(invocation, attribute, None)
            setattr(invocation, f"{attribute}_id", None)
            if _shared(session, snapshot):
                shared += 1
                continue
            reclaimed += snapshot.size
            payloads += 1
            session.delete(snapshot)

    session.flush()
    return TraceDeletion(
        project_id=project_id,
        payloads=payloads,
        bytes_reclaimed=reclaimed,
        records_kept=records,
        shared_payloads=shared,
    )


# ----------------------------------------------------------------------
# Assembling the document
# ----------------------------------------------------------------------


class _Counter:
    """How many payloads a sanitised export withheld."""

    def __init__(self) -> None:
        self.total = 0

    def hit(self) -> str:
        self.total += 1
        return WITHHELD


def _run(
    session: Session,
    snapshots: SnapshotStore,
    run: models.PipelineRun,
    *,
    sanitise: bool,
    withheld: _Counter,
) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status.value,
        "correlation_id": run.correlation_id,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "runtime_config": run.runtime_config,
        "executions": [
            _execution(snapshots, execution, sanitise=sanitise, withheld=withheld)
            for execution in run.stage_executions
        ],
    }


def _execution(
    snapshots: SnapshotStore,
    execution: models.StageExecution,
    *,
    sanitise: bool,
    withheld: _Counter,
) -> dict[str, Any]:
    return {
        "id": execution.id,
        "stage": execution.stage,
        "impl_version": execution.impl_version,
        "status": execution.status.value,
        "started_at": execution.started_at.isoformat(),
        "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
        "error_type": execution.error_type,
        "error_message": execution.error_message,
        "model_invocations": [
            _invocation(snapshots, invocation, sanitise=sanitise, withheld=withheld)
            for invocation in execution.model_invocations
        ],
        "decisions": [
            {
                "id": decision.id,
                "decision_type": decision.decision_type,
                "decided_by": decision.decided_by,
                "decided_by_type": decision.decided_by_type.value,
                "policy_version": decision.policy_version,
                "outcome": decision.outcome,
                "rationale": decision.rationale,
            }
            for decision in execution.decision_records
        ],
        "context_selections": [
            {
                "id": selection.id,
                "strategy": selection.strategy,
                "token_budget": selection.token_budget,
                "items": [
                    {
                        "reference": item.reference,
                        "disposition": item.disposition.value,
                        "reason": item.reason,
                        "score": item.score,
                    }
                    for item in selection.items
                ],
            }
            for selection in execution.context_selections
        ],
        "events": [
            {
                "sequence": event.sequence,
                "event_type": event.event_type,
                "actor_type": event.actor_type.value,
                "actor_id": event.actor_id,
                "timestamp": event.timestamp.isoformat(),
            }
            for event in execution.trace_events
        ],
    }


def _invocation(
    snapshots: SnapshotStore,
    invocation: models.ModelInvocation,
    *,
    sanitise: bool,
    withheld: _Counter,
) -> dict[str, Any]:
    """One model call: always its metadata, its payloads only if allowed."""
    return {
        "id": invocation.id,
        "attempt_ordinal": invocation.attempt_ordinal,
        "outcome": invocation.outcome.value,
        "retry_type": invocation.retry_type.value if invocation.retry_type else None,
        "provider": invocation.provider,
        "model": invocation.model,
        "template_id": invocation.template_id,
        "template_version": invocation.template_version,
        "retention_mode": invocation.retention_mode.value,
        "input_tokens": invocation.input_tokens,
        "output_tokens": invocation.output_tokens,
        "cost_usd": invocation.cost_usd,
        "started_at": invocation.started_at.isoformat(),
        "request": _payload(snapshots, invocation.request_snapshot, sanitise, withheld),
        "raw_response": _payload(snapshots, invocation.raw_response_snapshot, sanitise, withheld),
        "parsed_response": _payload(
            snapshots, invocation.parsed_response_snapshot, sanitise, withheld
        ),
        "validated_response": _payload(
            snapshots, invocation.validated_response_snapshot, sanitise, withheld
        ),
    }


def _payload(
    snapshots: SnapshotStore,
    snapshot: domain_models.ArtifactSnapshot | None,
    sanitise: bool,
    withheld: _Counter,
) -> Any:
    """The stored bytes, the withheld marker, or ``None`` if there is nothing.

    ``None`` and the marker are deliberately different: one says the payload was
    never kept (a retention mode, or a deletion), the other says it exists and
    this export is not carrying it.
    """
    if snapshot is None:
        return None
    if sanitise:
        return withheld.hit()
    try:
        return json.loads(snapshots.read(snapshot))
    except (ValueError, KeyError):
        # A payload that will not parse is still evidence; hand back the address
        # rather than dropping the record of it.
        return {"content_hash": snapshot.content_hash, "unreadable": True}


def _invocations(session: Session, project_id: str) -> list[models.ModelInvocation]:
    return list(
        session.scalars(
            select(models.ModelInvocation)
            .join(
                models.StageExecution,
                models.ModelInvocation.stage_execution_id == models.StageExecution.id,
            )
            .join(
                models.PipelineRun,
                models.StageExecution.pipeline_run_id == models.PipelineRun.id,
            )
            .where(models.PipelineRun.project_id == project_id)
        ).all()
    )


def _shared(session: Session, snapshot: domain_models.ArtifactSnapshot) -> bool:
    """Whether another snapshot row resolves to the same bytes."""
    others = session.scalars(
        select(domain_models.ArtifactSnapshot).where(
            domain_models.ArtifactSnapshot.content_hash == snapshot.content_hash,
            domain_models.ArtifactSnapshot.id != snapshot.id,
        )
    ).first()
    return others is not None


__all__ = [
    "WITHHELD",
    "ConfidentialExportRefused",
    "TraceDeletion",
    "TraceExport",
    "delete_traces",
    "export_traces",
]
