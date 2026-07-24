"""The single path every provenance record is written through (phase 03).

Why a single path: plan/00 requires redaction *before* persistence, and plan/03
requires the hook be wired into "the single persistence path all records pass
through". A rule applied at N call sites is a rule that will be missed at the
N+1th; a rule applied at one chokepoint cannot be. Every method here redacts its
payloads on the way in, so no caller can write an unredacted record without
bypassing the recorder entirely.

The clock and id factory are injected. Provenance assertions are about exact
stored values, and wall-clock timestamps with random ids would make the records
unassertable — the same reason the phase-01 LLM client is a scripted fake.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.provenance import models, schemas
from groundscribe.provenance.enums import (
    ActorType,
    ArtifactDirection,
    ExecutionStatus,
    InterventionType,
    InvocationOutcome,
    RetryType,
    ToolInitiator,
)
from groundscribe.provenance.redaction import Redactor
from groundscribe.provenance.schemas import ContextCandidate, EffectiveRequest
from groundscribe.storage.snapshot_store import SnapshotStore

#: A payload as handed to the recorder: structured, or raw provider text.
Payload = str | dict[str, Any]


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _default_id() -> str:
    return uuid.uuid4().hex


class ProvenanceRecorder:
    """Writes execution records, redacting every payload before it is persisted."""

    def __init__(
        self,
        session: Session,
        snapshots: SnapshotStore,
        *,
        redactor: Redactor | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session = session
        self._snapshots = snapshots
        self._redactor = redactor or Redactor()
        self._clock = clock or _default_clock
        self._new_id = id_factory or _default_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_run(
        self,
        *,
        project_id: str,
        correlation_id: str | None = None,
        runtime_config: dict[str, Any] | None = None,
    ) -> models.PipelineRun:
        """Open a pipeline run.

        The runtime configuration is captured at the start rather than read back
        later: a replay needs to know what the setup *was*, and by the time a run
        is inspected the configuration may have changed underneath it.
        """
        run = models.PipelineRun(
            id=self._new_id(),
            project_id=project_id,
            status=ExecutionStatus.RUNNING,
            correlation_id=correlation_id or self._new_id(),
            runtime_config=self._redactor.redact_payload(runtime_config or {}),
            started_at=self._clock(),
        )
        self._session.add(run)
        self._session.flush()
        return run

    def start_stage(
        self,
        run: models.PipelineRun,
        *,
        stage: str,
        ordinal: int = 0,
        parent: models.StageExecution | None = None,
    ) -> models.StageExecution:
        """Open a stage execution within ``run``, optionally branching from ``parent``."""
        execution = models.StageExecution(
            id=self._new_id(),
            pipeline_run=run,
            parent=parent,
            stage=stage,
            ordinal=ordinal,
            status=ExecutionStatus.RUNNING,
            correlation_id=run.correlation_id,
            started_at=self._clock(),
        )
        self._session.add(execution)
        self._session.flush()
        return execution

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------

    def record_model_invocation(
        self,
        execution: models.StageExecution,
        *,
        request: EffectiveRequest,
        provider: str,
        model: str,
        outcome: InvocationOutcome,
        raw_response: Payload | None = None,
        parsed_response: Payload | None = None,
        validated_response: Payload | None = None,
        parent: models.ModelInvocation | None = None,
        retry_type: RetryType | None = None,
        error_message: str | None = None,
    ) -> models.ModelInvocation:
        """Record one model call, including the ones that failed.

        ``parent`` and ``retry_type`` travel together and are rejected apart: a
        follow-up attempt that does not say why it exists is the bare-count
        modelling plan/03 rules out, and a first attempt claiming a retry type
        has nothing to retry.

        The three response forms become three separate snapshots, so a response
        that parses but fails validation is preserved rather than replaced.
        """
        if parent is not None and retry_type is None:
            raise ValueError("a follow-up attempt must state its retry_type")
        if parent is None and retry_type is not None:
            raise ValueError("retry_type requires a parent invocation to retry")

        invocation = models.ModelInvocation(
            id=self._new_id(),
            stage_execution=execution,
            parent=parent,
            attempt_ordinal=1 if parent is None else parent.attempt_ordinal + 1,
            retry_type=retry_type,
            outcome=outcome,
            provider=provider,
            model=model,
            template_id=request.template_id,
            template_version=request.template_version,
            request_snapshot=self._write_request(request, execution),
            raw_response_snapshot=self._write_optional(
                ArtifactType.RAW_RESPONSE, raw_response, execution
            ),
            parsed_response_snapshot=self._write_optional(
                ArtifactType.PARSED_RESPONSE, parsed_response, execution
            ),
            validated_response_snapshot=self._write_optional(
                ArtifactType.VALIDATED_RESPONSE, validated_response, execution
            ),
            started_at=self._clock(),
            completed_at=self._clock(),
            error_message=error_message,
        )
        self._session.add(invocation)
        self._session.flush()
        return invocation

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    def record_tool_invocation(
        self,
        execution: models.StageExecution,
        *,
        tool_name: str,
        tool_version: str,
        initiator: ToolInitiator,
        raw_args: dict[str, Any],
        normalised_args: dict[str, Any],
        raw_result: dict[str, Any],
        normalised_result: dict[str, Any],
        status: ExecutionStatus,
        model_invocation: models.ModelInvocation | None = None,
        approval_required: bool = False,
        approved_by: str | None = None,
        error_message: str | None = None,
    ) -> models.ToolInvocation:
        """Record a tool call with both arg/result forms and its authorisation."""
        tool = models.ToolInvocation(
            id=self._new_id(),
            stage_execution=execution,
            model_invocation=model_invocation,
            tool_name=tool_name,
            tool_version=tool_version,
            initiator=initiator,
            approval_required=approval_required,
            approved_by=approved_by,
            raw_args=self._redactor.redact_payload(raw_args),
            normalised_args=self._redactor.redact_payload(normalised_args),
            raw_result=self._redactor.redact_payload(raw_result),
            normalised_result=self._redactor.redact_payload(normalised_result),
            status=status,
            started_at=self._clock(),
            completed_at=self._clock(),
            error_message=error_message,
        )
        self._session.add(tool)
        self._session.flush()
        return tool

    def record_tool_dependency(
        self, tool: models.ToolInvocation, snapshot: ArtifactSnapshot
    ) -> None:
        """Record that ``snapshot`` rests on ``tool``'s result.

        This is the link that answers "if this fetch returned the wrong number,
        what else is wrong?" — a question no timestamp ordering can answer.
        """
        tool.dependents.append(snapshot)
        self._session.flush()

    # ------------------------------------------------------------------
    # Context, decisions, evaluations, interventions
    # ------------------------------------------------------------------

    def record_context_selection(
        self,
        execution: models.StageExecution,
        *,
        strategy: str,
        strategy_version: str,
        candidates: Sequence[ContextCandidate],
        token_budget: int | None = None,
    ) -> models.ContextSelection:
        """Record what was offered to the model and what became of each candidate.

        Excluded and truncated candidates are stored, not only selected ones:
        what the model could not see explains as many surprising outputs as what
        it could.
        """
        selection = models.ContextSelection(
            id=self._new_id(),
            stage_execution=execution,
            strategy=strategy,
            strategy_version=strategy_version,
            token_budget=token_budget,
        )
        selection.items = [
            models.ContextItem(
                id=self._new_id(),
                ordinal=ordinal,
                reference=candidate.reference,
                disposition=candidate.disposition,
                reason=self._redactor.redact_text(candidate.reason),
                score=candidate.score,
            )
            for ordinal, candidate in enumerate(candidates)
        ]
        self._session.add(selection)
        self._session.flush()
        return selection

    def record_decision(
        self,
        execution: models.StageExecution,
        *,
        decision_type: str,
        decided_by: str,
        decided_by_type: ActorType,
        outcome: str,
        policy_version: str | None = None,
        inputs: dict[str, Any] | None = None,
        rationale: str = "",
    ) -> models.DecisionRecord:
        """Record a decision, refusing to store one that cannot be attributed.

        Validation runs through the Pydantic schema rather than being restated
        here, so the writer and the wire format enforce the same rule.
        """
        record = schemas.DecisionRecord(
            id=self._new_id(),
            stage_execution_id=execution.id,
            decision_type=decision_type,
            decided_by=decided_by,
            decided_by_type=decided_by_type,
            policy_version=policy_version,
            inputs=self._redactor.redact_payload(inputs or {}),
            outcome=outcome,
            rationale=self._redactor.redact_text(rationale),
            decided_at=self._clock(),
        )
        decision = models.DecisionRecord(**record.model_dump())
        self._session.add(decision)
        self._session.flush()
        return decision

    def record_evaluation(
        self,
        execution: models.StageExecution,
        *,
        evaluator_id: str,
        evaluator_version: str,
        rubric_version: str,
        scores: dict[str, Any],
        passed: bool,
    ) -> models.EvaluationRun:
        """Record a scoring pass — its own record category, linked to the execution."""
        evaluation = models.EvaluationRun(
            id=self._new_id(),
            stage_execution=execution,
            evaluator_id=evaluator_id,
            evaluator_version=evaluator_version,
            rubric_version=rubric_version,
            scores=self._redactor.redact_payload(scores),
            passed=passed,
            created_at=self._clock(),
        )
        self._session.add(evaluation)
        self._session.flush()
        return evaluation

    def record_user_intervention(
        self,
        execution: models.StageExecution,
        *,
        user_id: str,
        intervention_type: InterventionType,
        payload: dict[str, Any] | None = None,
    ) -> models.UserIntervention:
        """Record a point where a human stepped into the run."""
        intervention = models.UserIntervention(
            id=self._new_id(),
            stage_execution=execution,
            user_id=user_id,
            intervention_type=intervention_type,
            payload=self._redactor.redact_payload(payload or {}),
            occurred_at=self._clock(),
        )
        self._session.add(intervention)
        self._session.flush()
        return intervention

    # ------------------------------------------------------------------
    # Trace
    # ------------------------------------------------------------------

    def emit(
        self,
        *,
        event_type: str,
        actor_type: ActorType,
        actor_id: str,
        payload: dict[str, Any] | None = None,
        run: models.PipelineRun | None = None,
        execution: models.StageExecution | None = None,
        caused_by: models.TraceEvent | None = None,
    ) -> models.TraceEvent:
        """Append one event to the run's timeline.

        The correlation id is taken from whichever anchor was supplied so every
        event of a run shares one, and the sequence number is assigned from the
        highest already stored for that correlation — a stored total order that
        does not depend on clock resolution.
        """
        anchor = run or (execution.pipeline_run if execution is not None else None)
        if anchor is None:
            raise ValueError("a trace event must be anchored to a run or a stage execution")

        event = models.TraceEvent(
            id=self._new_id(),
            pipeline_run=anchor,
            stage_execution=execution,
            event_type=event_type,
            timestamp=self._clock(),
            actor_type=actor_type,
            actor_id=actor_id,
            payload=self._redactor.redact_payload(payload or {}),
            correlation_id=anchor.correlation_id,
            causation_id=caused_by.id if caused_by is not None else None,
            sequence=self._next_sequence(anchor.correlation_id),
        )
        self._session.add(event)
        self._session.flush()
        return event

    def _next_sequence(self, correlation_id: str) -> int:
        highest = self._session.execute(
            select(func.max(models.TraceEvent.sequence)).where(
                models.TraceEvent.correlation_id == correlation_id
            )
        ).scalar_one()
        return 0 if highest is None else int(highest) + 1

    # ------------------------------------------------------------------
    # Artefacts consumed and produced
    # ------------------------------------------------------------------

    def record_input(
        self,
        execution: models.StageExecution,
        snapshot: ArtifactSnapshot,
        *,
        role: str = "",
    ) -> models.ExecutionArtifact:
        """Attach an existing snapshot as an input this execution consumed."""
        return self._attach(execution, snapshot, ArtifactDirection.INPUT, role)

    def record_output(
        self,
        execution: models.StageExecution,
        *,
        artifact_type: ArtifactType,
        content: Payload,
        role: str = "",
        parent: ArtifactSnapshot | None = None,
    ) -> ArtifactSnapshot:
        """Snapshot an artefact this execution produced and attach it as an output.

        The snapshot records ``created_by_execution_id`` here rather than leaving
        it to callers, which is what makes plan/00's "every artefact references a
        creating execution" true by construction on this path.
        """
        snapshot = self._write_snapshot(artifact_type, self._redact(content), execution, parent)
        self._attach(execution, snapshot, ArtifactDirection.OUTPUT, role)
        return snapshot

    def _attach(
        self,
        execution: models.StageExecution,
        snapshot: ArtifactSnapshot,
        direction: ArtifactDirection,
        role: str,
    ) -> models.ExecutionArtifact:
        artifact = models.ExecutionArtifact(
            id=self._new_id(),
            stage_execution=execution,
            snapshot=snapshot,
            direction=direction,
            role=role,
            ordinal=len(execution.artifacts),
        )
        self._session.add(artifact)
        self._session.flush()
        return artifact

    # ------------------------------------------------------------------
    # Payload persistence (the redaction chokepoint)
    # ------------------------------------------------------------------

    def _write_request(
        self, request: EffectiveRequest, execution: models.StageExecution
    ) -> ArtifactSnapshot:
        """Snapshot the redacted effective request, flagged as redacted."""
        payload = self._redactor.redact_payload(request.model_dump())
        payload["redacted"] = True
        return self._write_snapshot(ArtifactType.EFFECTIVE_REQUEST, payload, execution)

    def _write_optional(
        self,
        artifact_type: ArtifactType,
        payload: Payload | None,
        execution: models.StageExecution,
    ) -> ArtifactSnapshot | None:
        if payload is None:
            return None
        return self._write_snapshot(artifact_type, self._redact(payload), execution)

    def _redact(self, payload: Payload) -> Payload:
        """Redact a payload whether it arrived as structured data or raw text."""
        if isinstance(payload, str):
            return self._redactor.redact_text(payload)
        return self._redactor.redact_payload(payload)

    def _write_snapshot(
        self,
        artifact_type: ArtifactType,
        payload: Payload,
        execution: models.StageExecution,
        parent: ArtifactSnapshot | None = None,
    ) -> ArtifactSnapshot:
        """Serialise a *already redacted* payload into a content-addressed snapshot.

        Serialisation is canonical (sorted keys, no incidental whitespace) so two
        logically identical payloads hash identically and dedup — which is what
        makes a retry that resends the same request cost one blob, not two.
        """
        content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self._snapshots.write(
            artifact_type=artifact_type,
            content=content,
            created_by_execution_id=execution.id,
            parent=parent,
        )
