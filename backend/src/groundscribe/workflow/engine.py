"""The workflow engine: the machine, plus provenance and the artefact guards.

plan/05 → *decision-record emission*, and the guards that make plan/00's
invariants enforceable rather than aspirational: every artefact references a
creating execution, and no approved architecture mutates silently.

The engine owns three things the pure machine cannot:

1. **A decision record for every transition.** A state change nobody can
   attribute is a state change nobody can review, which is the failure this
   product exists to prevent.
2. **Guards that need stored artefacts.** Lineage, creating-execution links,
   content hashes and the exported bytes all live in the database; the machine
   deliberately cannot see them.
3. **Stage executions.** The engine opens them (and re-opens them on replay);
   phases 06-08 fill them with model calls.

Guards run *before* the machine moves and before anything is written. A guard
that fired afterwards would leave a decision record behind for a transition that
never happened, and no reader could tell that apart from one that did.

Deliberately not here: persistence of the workflow's own position. A run's state
lives in the engine for the length of the call; phase 09 owns the jobs table,
the worker, and the resumption that needs it stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType, InterventionType
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.errors import (
    ArtifactProvenanceError,
    AttributionRequired,
    ConfidentialMaterialError,
    ExportMismatchError,
    LineageError,
    SilentMutationError,
)
from groundscribe.workflow.machine import (
    RewriteApproval,
    RouteResult,
    StagnationCheck,
    TransitionOutcome,
    WorkflowMachine,
)
from groundscribe.workflow.policy import FailureCategory, WorkflowPolicy, default_workflow_policy
from groundscribe.workflow.stagnation import ScoreRound
from groundscribe.workflow.states import WorkflowAction, WorkflowState

#: The stage name the engine's own execution runs under. Transitions are the
#: engine's work, not the next stage's; attributing them to whichever stage
#: happened to follow would misplace every routing decision in the run.
WORKFLOW_STAGE = "workflow"


@dataclass(frozen=True)
class Override:
    """A person authorising a change to something already approved.

    ``requested_by`` is mandatory for the reason
    :class:`~groundscribe.workflow.machine.RewriteApproval` requires an
    approver: the override becomes a decision record, and phase 03 refuses to
    store a decision nobody is accountable for.
    """

    requested_by: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.requested_by:
            raise ValueError("requested_by is required: an anonymous override is unreviewable")


@dataclass(frozen=True)
class RecordedTransition:
    """A transition and the records written for it."""

    outcome: TransitionOutcome
    decision: models.DecisionRecord
    event: models.TraceEvent
    override: models.DecisionRecord | None = None

    @property
    def state(self) -> WorkflowState:
        return self.outcome.state

    @property
    def action(self) -> WorkflowAction:
        return self.outcome.action


@dataclass(frozen=True)
class RecordedRoute:
    """A routing decision, its record, and where the run ended up."""

    route: RouteResult
    decision: models.DecisionRecord
    event: models.TraceEvent

    @property
    def state(self) -> WorkflowState:
        return self.route.transition.state


@dataclass(frozen=True)
class RecordedStagnation:
    """A stagnation check; ``decision`` is ``None`` when nothing was found."""

    check: StagnationCheck
    decision: models.DecisionRecord | None = None
    event: models.TraceEvent | None = None


@dataclass
class _Approved:
    """What the engine must remember to keep its guards honest."""

    architecture: ArtifactSnapshot | None = None
    article_version: ArtifactSnapshot | None = None
    validated_version: ArtifactSnapshot | None = None


class WorkflowEngine:
    """Drives one pipeline run: applies transitions and records why."""

    def __init__(
        self,
        *,
        recorder: ProvenanceRecorder,
        snapshots: SnapshotStore,
        run: models.PipelineRun,
        state: WorkflowState = WorkflowState.SOURCE_INGESTED,
        policy: WorkflowPolicy | None = None,
        confidential: Sequence[str] = (),
        actor_id: str = "workflow_policy",
    ) -> None:
        self._recorder = recorder
        self._snapshots = snapshots
        self.run = run
        self.policy = policy or default_workflow_policy()
        self.machine = WorkflowMachine(state=state, policy=self.policy)
        self.execution = recorder.start_stage(run, stage=WORKFLOW_STAGE)
        self._confidential = tuple(confidential)
        self._actor_id = actor_id
        self._approved = _Approved()

    # ------------------------------------------------------------------
    # Position
    # ------------------------------------------------------------------

    @property
    def state(self) -> WorkflowState:
        return self.machine.state

    @property
    def is_paused(self) -> bool:
        return self.machine.is_paused

    @property
    def approved_architecture(self) -> ArtifactSnapshot | None:
        """The architecture currently approved, if one has been."""
        return self._approved.architecture

    @property
    def validated_version(self) -> ArtifactSnapshot | None:
        """The article version that passed final validation, if one has."""
        return self._approved.validated_version

    def available_actions(self) -> tuple[WorkflowAction, ...]:
        return self.machine.available_actions()

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def apply(
        self,
        action: WorkflowAction,
        *,
        target: WorkflowState | None = None,
        actor_id: str | None = None,
        actor_type: ActorType = ActorType.POLICY,
        rationale: str = "",
        artifacts: Sequence[ArtifactSnapshot] = (),
        override: Override | None = None,
    ) -> RecordedTransition:
        """Take ``action``, guarding it first and recording it afterwards.

        Order matters and is the whole design: attribute, guard, move, record.
        A guard that ran after the move would have to undo it, and one that ran
        after the write would leave a decision record for a transition that
        never happened.
        """
        decided_by = self._attribute(action, actor_id, actor_type)
        self._guard(action, artifacts, override)

        override_record = (
            self._record_override(action, artifacts, override) if override is not None else None
        )
        outcome = self.machine.apply(action, target=target, actor=actor_type)
        self._remember(action, artifacts)
        for snapshot in artifacts:
            self._recorder.record_input(self.execution, snapshot, role=action.value)

        decision, event = self._record_transition(
            outcome,
            decision_type="workflow_transition",
            decided_by=decided_by,
            decided_by_type=outcome.transition.actor,
            rationale=rationale or outcome.transition.rationale,
            inputs={"artifacts": [snapshot.id for snapshot in artifacts]},
        )
        return RecordedTransition(
            outcome=outcome, decision=decision, event=event, override=override_record
        )

    def route(
        self,
        category: FailureCategory,
        *,
        prefer: WorkflowState | None = None,
        approval: RewriteApproval | None = None,
    ) -> RecordedRoute:
        """Route a failing article, recording the policy or person behind it.

        An approved extra round is attributed to the person who approved it, not
        to the policy they overrode — the policy did not make this choice, and
        recording it as though it did would misdirect the next person asking why
        this run went further than the limits allow. The same reasoning as
        phase 04's model-routing override.
        """
        result = self.machine.route(category, prefer=prefer, approval=approval)
        approver = result.approval
        decision, event = self._record_transition(
            result.transition,
            decision_type="revision_routing",
            decided_by=approver.approved_by if approver is not None else self._actor_id,
            decided_by_type=ActorType.USER if approver is not None else ActorType.POLICY,
            rationale=(
                approver.reason
                if approver is not None
                else (result.reason or result.outcome.rationale)
            ),
            inputs={
                "category": category.value,
                "requested_target": result.outcome.target.value,
                "limit": result.outcome.limit.value if result.outcome.limit else None,
                "escalated": result.escalated,
            },
        )
        if result.escalated:
            self._request_intervention(
                reason=result.reason,
                payload={
                    "category": category.value,
                    "limit": result.outcome.limit.value if result.outcome.limit else None,
                },
            )
        return RecordedRoute(route=result, decision=decision, event=event)

    def check_stagnation(self, history: Sequence[ScoreRound]) -> RecordedStagnation:
        """Stall the run if the loop has stopped improving, recording why."""
        check = self.machine.check_stagnation(history)
        if check.transition is None:
            return RecordedStagnation(check=check)

        decision, event = self._record_transition(
            check.transition,
            decision_type="stagnation",
            decided_by=self._actor_id,
            decided_by_type=ActorType.POLICY,
            rationale="; ".join(finding.detail for finding in check.findings),
            inputs={
                "signals": [finding.signal.value for finding in check.findings],
                "evidence": [finding.evidence for finding in check.findings],
            },
        )
        self._request_intervention(
            reason="the revision loop has stopped improving",
            payload={"signals": [finding.signal.value for finding in check.findings]},
        )
        return RecordedStagnation(check=check, decision=decision, event=event)

    # ------------------------------------------------------------------
    # Stage executions
    # ------------------------------------------------------------------

    def begin_stage(
        self,
        stage: str,
        *,
        impl_version: str = "",
        parent: models.StageExecution | None = None,
    ) -> models.StageExecution:
        """Open a stage execution under this run for phases 06-08 to fill."""
        return self._recorder.start_stage(
            self.run, stage=stage, impl_version=impl_version, parent=parent
        )

    def replay(
        self, execution: models.StageExecution, *, requested_by: str
    ) -> models.StageExecution:
        """Re-run a stage as a *new* execution branched from the original.

        The original is never touched. plan/05 states the rule as "replays
        cannot overwrite original executions", and the reason is the whole
        provenance guarantee: a replay that edited its predecessor would destroy
        the record of what the first run actually did — which is the only thing
        worth comparing the replay against.
        """
        replayed = self._recorder.start_stage(
            self.run, stage=execution.stage, ordinal=execution.ordinal, parent=execution
        )
        self._recorder.record_decision(
            self.execution,
            decision_type="execution_replay",
            decided_by=requested_by,
            decided_by_type=ActorType.USER,
            inputs={"original_execution_id": execution.id, "replay_execution_id": replayed.id},
            outcome=replayed.id,
            rationale="replayed as a new linked execution; the original is unchanged",
        )
        return replayed

    def fail(
        self,
        *,
        error_type: str,
        error_message: str,
        execution: models.StageExecution | None = None,
    ) -> RecordedTransition:
        """Fail a stage and the run, keeping everything recorded so far.

        The stage is failed through the recorder, which writes rather than rolls
        back (phase 03), so the invocations and events that explain the failure
        survive it.
        """
        self._recorder.fail_stage(
            execution or self.execution, error_type=error_type, error_message=error_message
        )
        return self.apply(
            WorkflowAction.FAIL,
            actor_id=self._actor_id,
            actor_type=ActorType.SYSTEM,
            rationale=f"{error_type}: {error_message}",
        )

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _attribute(
        self, action: WorkflowAction, actor_id: str | None, actor_type: ActorType
    ) -> str:
        """Who is taking this action; refuse an anonymous human one."""
        if actor_type is ActorType.USER and not actor_id:
            raise AttributionRequired(
                f"{action.value} is a user action and needs an actor_id: "
                "an unattributed decision cannot be reviewed"
            )
        return actor_id or self._actor_id

    def _guard(
        self,
        action: WorkflowAction,
        artifacts: Sequence[ArtifactSnapshot],
        override: Override | None,
    ) -> None:
        """Every artefact rule, checked before the machine moves."""
        for snapshot in artifacts:
            if snapshot.created_by_execution_id is None:
                raise ArtifactProvenanceError(
                    f"snapshot {snapshot.id} ({snapshot.artifact_type.value}) has no creating "
                    "execution; the transition that produced it cannot be reconstructed"
                )

        self._guard_architecture(artifacts, override)
        self._guard_article_lineage(artifacts)
        if action is WorkflowAction.APPROVE_FINAL:
            self._guard_export(artifacts)

    def _guard_architecture(
        self, artifacts: Sequence[ArtifactSnapshot], override: Override | None
    ) -> None:
        """plan/05: no approved architecture changes silently.

        Two pieces of evidence are required, and neither substitutes for the
        other: the new snapshot forking from the approved one shows *what*
        changed, and the override record shows *who* decided it should. An
        override without lineage is a replacement wearing a signature.
        """
        approved = self._approved.architecture
        if approved is None:
            return
        for snapshot in artifacts:
            if snapshot.artifact_type is not ArtifactType.CONTENT_ARCHITECTURE:
                continue
            if snapshot.id == approved.id:
                continue
            if snapshot.parent_snapshot_id != approved.id:
                raise SilentMutationError(
                    f"architecture {snapshot.id} does not fork from the approved "
                    f"{approved.id}; an approved architecture is superseded, never replaced"
                )
            if override is None:
                raise SilentMutationError(
                    f"architecture {snapshot.id} supersedes an approved architecture and "
                    "needs an override naming who authorised it"
                )

    def _guard_article_lineage(self, artifacts: Sequence[ArtifactSnapshot]) -> None:
        """plan/05: every article version retains lineage."""
        previous = self._approved.article_version
        if previous is None:
            return
        for snapshot in artifacts:
            if snapshot.artifact_type is not ArtifactType.ARTICLE_VERSION:
                continue
            if snapshot.id != previous.id and snapshot.parent_snapshot_id is None:
                raise LineageError(
                    f"article version {snapshot.id} supersedes {previous.id} but records no "
                    "parent; lineage is what makes a rewrite comparable to what it replaced"
                )

    def _guard_export(self, artifacts: Sequence[ArtifactSnapshot]) -> None:
        """plan/05: export the validated version, and nothing confidential."""
        validated = self._approved.validated_version
        versions = [
            snapshot
            for snapshot in artifacts
            if snapshot.artifact_type is ArtifactType.ARTICLE_VERSION
        ]
        if validated is None:
            raise ExportMismatchError(
                "no version has passed final validation; there is nothing approvable yet"
            )
        for snapshot in versions:
            if snapshot.id != validated.id:
                raise ExportMismatchError(
                    f"version {snapshot.id} is not the version that passed validation "
                    f"({validated.id}); export must use the version that was checked"
                )
        self._guard_confidential(versions or [validated])

    def _guard_confidential(self, versions: Sequence[ArtifactSnapshot]) -> None:
        """plan/05 engine-level guard; phase 13 enforces this end to end.

        A substring scan of the stored bytes, deliberately crude. It is the last
        gate before something is published, and the cost of a false positive —
        a person looking at the draft again — is nothing beside the cost of the
        miss it is here to prevent.
        """
        if not self._confidential:
            return
        for snapshot in versions:
            body = self._snapshots.read(snapshot).decode("utf-8", errors="replace")
            for marker in self._confidential:
                if marker in body:
                    raise ConfidentialMaterialError(
                        f"version {snapshot.id} contains confidential material ({marker!r}) "
                        "and cannot be published"
                    )

    def _remember(self, action: WorkflowAction, artifacts: Sequence[ArtifactSnapshot]) -> None:
        """Record what this transition makes authoritative for later guards."""
        for snapshot in artifacts:
            if snapshot.artifact_type is ArtifactType.CONTENT_ARCHITECTURE:
                # Either an approval or a supersession that already cleared
                # `_guard_architecture`; in both cases the approval follows the
                # version now in force, which is what the next guard compares to.
                self._approved.architecture = snapshot
            elif snapshot.artifact_type is ArtifactType.ARTICLE_VERSION:
                self._approved.article_version = snapshot
                if action is WorkflowAction.VALIDATION_PASSED:
                    self._approved.validated_version = snapshot

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record_transition(
        self,
        outcome: TransitionOutcome,
        *,
        decision_type: str,
        decided_by: str,
        decided_by_type: ActorType,
        rationale: str,
        inputs: dict[str, Any] | None = None,
    ) -> tuple[models.DecisionRecord, models.TraceEvent]:
        """One decision record and one trace event per transition.

        Both, not either. The decision is the reviewable artefact — who moved
        the run and under which policy — while the event is what a timeline or
        an SSE stream reads; a decision alone would leave phase 09 querying
        decision rows to render progress.
        """
        decision = self._recorder.record_decision(
            self.execution,
            decision_type=decision_type,
            decided_by=decided_by,
            decided_by_type=decided_by_type,
            policy_version=(self.policy.version if decided_by_type is ActorType.POLICY else None),
            inputs={
                "from": outcome.previous_state.value,
                "action": outcome.action.value,
                **(inputs or {}),
            },
            outcome=outcome.state.value,
            rationale=rationale,
        )
        event = self._recorder.emit(
            event_type="workflow.transitioned",
            actor_type=decided_by_type,
            actor_id=decided_by,
            execution=self.execution,
            payload={
                "from": outcome.previous_state.value,
                "to": outcome.state.value,
                "action": outcome.action.value,
                "decision_record_id": decision.id,
            },
        )
        return decision, event

    def _record_override(
        self,
        action: WorkflowAction,
        artifacts: Sequence[ArtifactSnapshot],
        override: Override,
    ) -> models.DecisionRecord:
        """Record who authorised superseding something already approved.

        Written as a decision *and* a user intervention: the decision explains
        the change, the intervention is what the human-control-points view in
        phase 11 reads. They answer different questions about the same act.
        """
        self._recorder.record_user_intervention(
            self.execution,
            user_id=override.requested_by,
            intervention_type=InterventionType.OVERRIDE,
            payload={"action": action.value, "reason": override.reason},
        )
        return self._recorder.record_decision(
            self.execution,
            decision_type="architecture_override",
            decided_by=override.requested_by,
            decided_by_type=ActorType.USER,
            inputs={"artifacts": [snapshot.id for snapshot in artifacts]},
            outcome="approved_artifact_superseded",
            rationale=override.reason,
        )

    def _request_intervention(self, *, reason: str, payload: dict[str, Any]) -> None:
        """Announce that the run needs a person, for phase 09's queue to find."""
        self._recorder.emit(
            event_type="intervention.requested",
            actor_type=ActorType.SYSTEM,
            actor_id=self._actor_id,
            execution=self.execution,
            payload={"reason": reason, **payload},
        )


__all__ = [
    "WORKFLOW_STAGE",
    "Override",
    "RecordedRoute",
    "RecordedStagnation",
    "RecordedTransition",
    "WorkflowEngine",
]
