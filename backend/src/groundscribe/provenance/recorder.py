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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.provenance import models
from groundscribe.provenance.enums import ExecutionStatus, InvocationOutcome, RetryType
from groundscribe.provenance.redaction import Redactor
from groundscribe.provenance.schemas import EffectiveRequest
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
        )
