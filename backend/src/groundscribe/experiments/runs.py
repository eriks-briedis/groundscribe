"""Running a baseline against its candidates, and reading the answer (phase 12).

plan/12 → *ExperimentRun: baseline vs one+ candidate configurations over an
evaluation dataset; per-example results, aggregate comparison, human-preference
decisions.*

**An arm is a fork.** plan/12 already calls fork the primary improvement
mechanism; an experiment that invented a second way to vary a configuration would
hold two definitions of what a candidate is, and the two would drift apart on the
day one of them gained a variable. So an arm is a label plus
:class:`~groundscribe.experiments.variables.ForkVariables`, and starting an
experiment queues the same job a fork request would.

**The baseline is an arm and runs like one.** Reusing the numbers the original
execution already recorded would be cheaper, and it would compare a candidate
against a different draw from a nondeterministic model as well as against a
different configuration. Phase 12's reproducibility contract says exactly this
about replay; an experiment is where the point stops being academic.

**Two arms are not one request made twice.** The replay endpoint deduplicates by
source execution — right for a person clicking a button twice — so an experiment
keys its jobs by arm and entry as well. Without that, the queue would hand the
second arm the first arm's job and the comparison would report that two
configurations agreed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.experiments.datasets import DatasetBuilder
from groundscribe.experiments.edit_distance import ManualEditDistance, measure_manual_edit
from groundscribe.experiments.metrics import ArmMetrics, ExampleEvidence, aggregate_arm
from groundscribe.experiments.models import (
    EvaluationDataset,
    EvaluationDatasetEntry,
    ExperimentArm,
    ExperimentPreference,
    ExperimentResult,
)
from groundscribe.experiments.replay import Rerun, plan_rerun
from groundscribe.experiments.variables import ForkVariables
from groundscribe.jobs.enums import JobStatus
from groundscribe.jobs.models import Job
from groundscribe.jobs.queue import JobQueue
from groundscribe.provenance import models
from groundscribe.provenance.enums import ExecutionStatus, InvocationOutcome
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.validation.checks import ValidationCheck
from groundscribe.workflow.position import WorkflowPosition
from groundscribe.workflow.states import WorkflowState


class UnknownArm(LookupError):
    """A judgement was filed against an arm this experiment does not have."""


class IncomparableExperiment(ValueError):
    """An experiment was described in a way that could not produce a comparison.

    A ``ValueError`` because the request is malformed rather than the system
    being in the wrong state — which is also the distinction the API's status
    map draws between 422 and 409.
    """


@dataclass(frozen=True)
class ArmSpec:
    """One configuration to put under test.

    The baseline carries no variables, which is what makes it the baseline: it is
    the configuration the corpus was produced under, run again.
    """

    label: str
    variables: ForkVariables = field(default_factory=ForkVariables)
    baseline: bool = False


class ExperimentRunner:
    """Opens experiments, queues their arms, and reads the comparison back."""

    def __init__(
        self,
        session: Session,
        *,
        queue: JobQueue,
        snapshots: SnapshotStore,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session = session
        self._queue = queue
        self._snapshots = snapshots
        self._clock = clock or (lambda: datetime.now(UTC))
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)

    # ------------------------------------------------------------------
    # Opening and running
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        dataset: EvaluationDataset,
        created_by: str,
        arms: Sequence[ArmSpec],
        description: str = "",
    ) -> models.ExperimentRun:
        """Open an experiment with its arms, refusing one with nothing to compare against.

        Refused here rather than at aggregation, where the table would simply
        come out with no row marked as the thing everything else is measured
        from — and be read as though the first arm were it.
        """
        if not any(arm.baseline for arm in arms):
            raise IncomparableExperiment(
                "an experiment needs a baseline arm; a comparison with nothing to compare "
                "against reports differences from whichever arm was listed first"
            )

        experiment = models.ExperimentRun(
            id=self._new_id(),
            name=name,
            description=description,
            status=ExecutionStatus.PENDING,
            dataset_id=dataset.id,
            created_by=created_by,
            created_at=self._clock(),
        )
        self._session.add(experiment)
        self._session.flush()

        for ordinal, spec in enumerate(arms):
            self._session.add(
                ExperimentArm(
                    id=self._new_id(),
                    experiment_id=experiment.id,
                    label=spec.label,
                    baseline=spec.baseline,
                    ordinal=ordinal,
                    variables=spec.variables.changes,
                )
            )
        self._session.flush()
        return experiment

    def start(self, experiment: models.ExperimentRun) -> tuple[ExperimentResult, ...]:
        """Queue every arm against every example, and record a result for each.

        Written before anything runs, because an experiment that recorded only
        what completed could not tell "not run yet" from "ran and produced
        nothing" — and the second is a finding about the candidate.
        """
        experiment.status = ExecutionStatus.RUNNING
        results: list[ExperimentResult] = []
        for arm in self.arms(experiment):
            variables = ForkVariables.model_validate(arm.variables)
            for entry in self.entries(experiment):
                results.append(self._queue_arm(experiment, arm, entry, variables))
        self._session.flush()
        return tuple(results)

    def collect(self, experiment: models.ExperimentRun) -> tuple[ExperimentResult, ...]:
        """Bring each result up to date with the job that was running it.

        A separate step from :meth:`start` because the work happens in another
        process: the experiment is a queue of jobs, and reading it is asking
        where they got to.
        """
        results = self.results(experiment)
        for result in results:
            job = self._session.get(Job, result.job_id) if result.job_id else None
            if job is None:
                continue
            result.stage_execution_id = job.stage_execution_id
            result.status = _STATUS_OF.get(job.status, ExecutionStatus.RUNNING)
            result.error_message = job.error_message

        if all(result.status in _FINISHED for result in results) and results:
            experiment.status = (
                ExecutionStatus.SUCCEEDED
                if all(result.status is ExecutionStatus.SUCCEEDED for result in results)
                else ExecutionStatus.FAILED
            )
            experiment.completed_at = self._clock()
        self._session.flush()
        return results

    # ------------------------------------------------------------------
    # Judging
    # ------------------------------------------------------------------

    def prefer(
        self,
        experiment: models.ExperimentRun,
        *,
        entry: EvaluationDatasetEntry,
        arm: ExperimentArm,
        decided_by: str,
        reason: str = "",
    ) -> ExperimentPreference:
        """Record which arm a person judged better on one example.

        The arm is checked against this experiment. A judgement filed against the
        wrong comparison would count toward a preference rate for an arm nobody
        was shown, in an experiment nobody was asked about.
        """
        if arm.experiment_id != experiment.id:
            raise UnknownArm(
                f"arm {arm.id} belongs to experiment {arm.experiment_id}, not {experiment.id}"
            )
        preference = ExperimentPreference(
            id=self._new_id(),
            experiment_id=experiment.id,
            entry_id=entry.id,
            preferred_arm_id=arm.id,
            decided_by=decided_by,
            reason=reason,
            decided_at=self._clock(),
        )
        self._session.add(preference)
        self._session.flush()
        return preference

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def compare(self, experiment: models.ExperimentRun) -> tuple[ArmMetrics, ...]:
        """The aggregate table: one row per arm, in the order they were declared."""
        preferences = {
            (preference.entry_id, preference.preferred_arm_id)
            for preference in self.preferences(experiment)
        }
        judged = {entry_id for entry_id, _ in preferences}
        results = self.results(experiment)
        return tuple(
            aggregate_arm(
                arm_id=arm.id,
                label=arm.label,
                baseline=arm.baseline,
                evidence=tuple(
                    self._evidence(result, judged=judged, preferences=preferences)
                    for result in results
                    if result.arm_id == arm.id
                ),
            )
            for arm in self.arms(experiment)
        )

    def arms(self, experiment: models.ExperimentRun) -> tuple[ExperimentArm, ...]:
        return tuple(
            self._session.scalars(
                select(ExperimentArm)
                .where(ExperimentArm.experiment_id == experiment.id)
                .order_by(ExperimentArm.ordinal)
            )
        )

    def results(self, experiment: models.ExperimentRun) -> tuple[ExperimentResult, ...]:
        return tuple(
            self._session.scalars(
                select(ExperimentResult)
                .where(ExperimentResult.experiment_id == experiment.id)
                .order_by(ExperimentResult.id)
            )
        )

    def preferences(self, experiment: models.ExperimentRun) -> tuple[ExperimentPreference, ...]:
        return tuple(
            self._session.scalars(
                select(ExperimentPreference).where(
                    ExperimentPreference.experiment_id == experiment.id
                )
            )
        )

    def entries(self, experiment: models.ExperimentRun) -> tuple[EvaluationDatasetEntry, ...]:
        return tuple(
            self._session.scalars(
                select(EvaluationDatasetEntry)
                .where(EvaluationDatasetEntry.dataset_id == experiment.dataset_id)
                .order_by(EvaluationDatasetEntry.ordinal)
            )
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _queue_arm(
        self,
        experiment: models.ExperimentRun,
        arm: ExperimentArm,
        entry: EvaluationDatasetEntry,
        variables: ForkVariables,
    ) -> ExperimentResult:
        """One arm against one example, as a job and the row that watches it."""
        execution = entry.stage_execution
        job_type, payload = plan_rerun(
            self._session,
            execution,
            Rerun(
                source_execution_id=execution.id,
                requested_by=experiment.created_by or "experiment",
                reason=f"{experiment.name}: {arm.label}",
                variables=variables,
            ),
        )
        job = self._queue.enqueue(
            job_type=job_type,
            run=execution.pipeline_run,
            payload=payload,
            # Arm and entry, not just the execution. The replay endpoint's key is
            # "this execution, again", which two arms of one experiment would
            # share — and the second arm would be handed the first arm's job.
            dedupe_key=f"experiment:{experiment.id}:{arm.id}:{entry.id}",
        )
        result = ExperimentResult(
            id=self._new_id(),
            experiment_id=experiment.id,
            arm_id=arm.id,
            entry_id=entry.id,
            job_id=job.id,
            status=ExecutionStatus.PENDING,
        )
        self._session.add(result)
        return result

    def _evidence(
        self,
        result: ExperimentResult,
        *,
        judged: set[str],
        preferences: set[tuple[str, str]],
    ) -> ExampleEvidence:
        """Everything the metrics need about one arm's run of one example."""
        execution = result.stage_execution
        if execution is None:
            return ExampleEvidence(
                entry_id=result.entry_id,
                succeeded=False,
                decided=result.entry_id in judged,
                preferred=(result.entry_id, result.arm_id) in preferences,
            )

        evaluation = execution.evaluation_runs[0] if execution.evaluation_runs else None
        scores = dict(evaluation.scores) if evaluation is not None else {}
        confidence = scores.get("confidence") or {}
        position = self._position_of(execution)

        return ExampleEvidence(
            entry_id=result.entry_id,
            succeeded=result.status is ExecutionStatus.SUCCEEDED,
            scored=evaluation is not None,
            passed=bool(evaluation.passed) if evaluation is not None else False,
            unsupported_claims=len(scores.get("unsupported_claims") or ()),
            score_dispersion=_as_float(confidence.get("dispersion")),
            revision_rounds=_rounds(position),
            stagnated=self._stagnated(execution),
            accepted=position is not None and position.state is WorkflowState.COMPLETED,
            cost_usd=_cost(execution),
            latency_ms=_duration_ms(execution),
            model_calls=len(execution.model_invocations),
            schema_failures=sum(
                1 for call in execution.model_invocations if call.outcome in _SCHEMA_FAILURES
            ),
            confidentiality_failures=self._confidentiality_failures(execution),
            edit_distance=self._edit_distance(result, execution),
            decided=result.entry_id in judged,
            preferred=(result.entry_id, result.arm_id) in preferences,
        )

    def _edit_distance(
        self, result: ExperimentResult, execution: models.StageExecution
    ) -> ManualEditDistance | None:
        """How far this arm's article sits from the one the author approved.

        plan/12 defines the manual edit distance as the difference between what
        the pipeline proposed and what the author approved. Inside an experiment
        the approved version *is* the dataset entry, which is the thing that
        makes keeping the corpus worthwhile.
        """
        produced = next(
            (
                artefact.snapshot
                for artefact in execution.outputs
                if artefact.snapshot is not None
                and artefact.snapshot.artifact_type is ArtifactType.ARTICLE_VERSION
            ),
            None,
        )
        if produced is None:
            return None
        builder = DatasetBuilder(self._session, snapshots=self._snapshots)
        approved = builder.reference(result.entry)
        return measure_manual_edit(
            _body_of(self._snapshots, produced), str(approved.get("body", ""))
        )

    def _position_of(self, execution: models.StageExecution) -> WorkflowPosition | None:
        return self._session.scalars(
            select(WorkflowPosition).where(
                WorkflowPosition.pipeline_run_id == execution.pipeline_run_id
            )
        ).first()

    def _stagnated(self, execution: models.StageExecution) -> bool:
        """Whether this run was ever found to be going nowhere.

        Read from the decision records of the whole run rather than this one
        execution: stagnation is a property of a loop, and the execution that
        detected it is rarely the one an arm re-ran.
        """
        return (
            self._session.scalars(
                select(models.DecisionRecord)
                .join(
                    models.StageExecution,
                    models.StageExecution.id == models.DecisionRecord.stage_execution_id,
                )
                .where(
                    models.StageExecution.pipeline_run_id == execution.pipeline_run_id,
                    models.DecisionRecord.decision_type == "stagnation",
                    models.DecisionRecord.outcome != "none",
                )
            ).first()
            is not None
        )

    def _confidentiality_failures(self, execution: models.StageExecution) -> int:
        """How many times validation caught confidential material in this run."""
        return sum(
            1
            for record in self._session.scalars(
                select(models.DecisionRecord)
                .join(
                    models.StageExecution,
                    models.StageExecution.id == models.DecisionRecord.stage_execution_id,
                )
                .where(
                    models.StageExecution.pipeline_run_id == execution.pipeline_run_id,
                    models.DecisionRecord.decision_type == "final_validation",
                )
            )
            if ValidationCheck.CONFIDENTIAL_NAMES.value
            in str(record.inputs.get("failed_checks", ""))
        )


#: How a finished job's status reads as an execution status.
_STATUS_OF: dict[JobStatus, ExecutionStatus] = {
    JobStatus.PENDING: ExecutionStatus.PENDING,
    JobStatus.RUNNING: ExecutionStatus.RUNNING,
    JobStatus.SUCCEEDED: ExecutionStatus.SUCCEEDED,
    JobStatus.FAILED: ExecutionStatus.FAILED,
    JobStatus.CANCELLED: ExecutionStatus.FAILED,
}

_FINISHED = frozenset({ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED})

#: The outcomes plan/12 counts as a schema failure: a response the pipeline could
#: not parse, and one it parsed but could not accept.
_SCHEMA_FAILURES = frozenset({InvocationOutcome.INVALID_JSON, InvocationOutcome.INVALID_SCHEMA})


def _rounds(position: WorkflowPosition | None) -> int:
    if position is None:
        return 0
    return sum(int(value) for value in position.rounds.values())


def _cost(execution: models.StageExecution) -> float | None:
    costs = [call.cost_usd for call in execution.model_invocations if call.cost_usd is not None]
    return sum(costs) if costs else None


def _duration_ms(execution: models.StageExecution) -> int | None:
    if execution.completed_at is None:
        return None
    return int((execution.completed_at - execution.started_at).total_seconds() * 1000)


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _body_of(snapshots: SnapshotStore, snapshot: ArtifactSnapshot) -> str:
    """The prose one stored version holds, whichever stage wrote it."""
    payload = json.loads(snapshots.read(snapshot))
    return str(payload.get("body", ""))


__all__ = ["ArmSpec", "ExperimentRunner", "IncomparableExperiment", "UnknownArm"]
