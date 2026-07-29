"""The seventeen numbers plan/14 asks an installation to expose.

plan/14 → *Observability surface: the spec's metrics (stage duration, token
usage, estimated cost, retry count, validation failures, schema-repair frequency,
score change, rewrite count, accepted/rejected issues, stagnation frequency,
override frequency, question response rate, model fallback frequency, context
truncation frequency, tool failure frequency, human edit distance, final approval
rate) exposed*.

**A metric is a query over the trace, not a counter beside it.** No gauge is
incremented anywhere in this system. Every number here is computed from
provenance rows the pipeline already wrote, which is the only arrangement in
which a metric cannot quietly disagree with the record it summarises. The cost is
that a metric is as expensive as its query; the benefit is that any number can be
argued with by opening the same rows it read, which for a product whose
proposition is *inspectable provenance* is not a close trade.

**Nothing observed is ``None``, not zero** — phase 12's rule, carried forward. A
rate reports ``None`` until there is something to divide, because an installation
that has never called a tool reporting a 0% tool-failure rate is stating a fact it
does not have. Counts stay zero: "no stage has run" is honestly zero.

**Each denominator is chosen rather than inherited.** Stagnation is per run
because a *run* stalls; overrides are per human intervention because the question
is how often a person had to overrule the system, not how often they touched it;
the final approval rate is measured against runs that reached the gate, because a
project nobody has looked at yet is not a rejection.

Split in two as plan/12's experiment metrics are: :class:`RunObservations` is what
the rows say, :func:`summarise` is arithmetic over it, and :func:`collect_metrics`
is the one function that touches a database. The seam means every rate can be
checked against numbers written by hand.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import fmean
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import FindingStatus
from groundscribe.experiments.edit_distance import measure_manual_edit
from groundscribe.provenance import models as provenance_models
from groundscribe.provenance.enums import (
    ContextDisposition,
    ExecutionStatus,
    InterventionType,
    RetryType,
)
from groundscribe.scoring.scoring import SCORE_STAGE
from groundscribe.stages.rewriting import REWRITE_STAGE
from groundscribe.voice.models import ManualEdit
from groundscribe.workflow.states import WorkflowState

#: The metrics plan/14 names, as the fields that must appear on the surface.
#:
#: A tuple rather than a comment: a metric dropped from the model fails a test by
#: name instead of vanishing from every dashboard that renders it.
METRIC_NAMES: tuple[str, ...] = (
    "stage_durations",
    "token_usage",
    "estimated_cost_usd",
    "retry_count",
    "validation_failures",
    "schema_repair_frequency",
    "score_change",
    "rewrite_count",
    "issue_decisions",
    "stagnation_frequency",
    "override_frequency",
    "question_response_rate",
    "model_fallback_frequency",
    "context_truncation_frequency",
    "tool_failure_frequency",
    "human_edit_distance",
    "final_approval_rate",
)

#: Why a follow-up call was made, when the reason was the response not conforming.
#:
#: Both, because plan/03 separates them deliberately: ``INVALID_SCHEMA`` is a
#: response that did not fit, ``CONTENT_REPAIR`` is one that fitted and said
#: something unusable. A "schema-repair frequency" that counted only the first
#: would under-report exactly the failures that cost the most to fix.
REPAIR_RETRIES = frozenset({RetryType.INVALID_SCHEMA, RetryType.CONTENT_REPAIR})


class StageDuration(BaseModel):
    """How long one stage takes, over every time it has run."""

    model_config = ConfigDict(frozen=True)

    stage: str
    executions: int
    total_ms: int
    mean_ms: float


class TokenTotals(BaseModel):
    """What the installation has asked models to read and write."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class IssueDecisions(BaseModel):
    """What became of the findings a reviewer raised.

    plan/14 names "accepted/rejected issues"; the row has five states, and the
    other three are reported rather than folded in. A suppressed finding is the
    *system* holding back a criticism the author already dismissed and an edited
    one is the author rewriting it — counting either as accepted or rejected
    would answer a question nobody asked with a number nobody could check.
    """

    model_config = ConfigDict(frozen=True)

    proposed: int = 0
    accepted: int = 0
    rejected: int = 0
    edited: int = 0
    suppressed: int = 0


class RunMetrics(BaseModel):
    """The observability surface for one project, or for the whole installation."""

    model_config = ConfigDict(frozen=True)

    #: What the numbers are about. ``None`` means every project.
    project_id: str | None = None
    #: How many pipeline runs are in scope — the denominator behind several of
    #: the rates below, reported so a reader never has to guess at it.
    runs: int = 0

    stage_durations: tuple[StageDuration, ...] = ()
    token_usage: TokenTotals = TokenTotals()
    estimated_cost_usd: float | None = None
    retry_count: int = 0
    validation_failures: int = 0
    schema_repair_frequency: float | None = None
    score_change: float | None = None
    rewrite_count: int = 0
    issue_decisions: IssueDecisions = IssueDecisions()
    stagnation_frequency: float | None = None
    override_frequency: float | None = None
    question_response_rate: float | None = None
    model_fallback_frequency: float | None = None
    context_truncation_frequency: float | None = None
    tool_failure_frequency: float | None = None
    human_edit_distance: float | None = None
    final_approval_rate: float | None = None


@dataclass(frozen=True)
class RunObservations:
    """Everything the metrics need, as flat facts read off the rows.

    A record rather than a handle on the database, for the reason phase 12 gave
    :class:`~groundscribe.experiments.metrics.ExampleEvidence`: gathering and
    arithmetic have different failure modes, and a rate that reached for its own
    rows could not be checked against numbers written by hand.
    """

    project_id: str | None = None
    runs: int = 0

    #: ``(stage, duration_ms)`` for every execution that finished.
    stage_runs: tuple[tuple[str, int], ...] = ()

    input_tokens: int = 0
    output_tokens: int = 0
    #: Only the calls that reported a cost. An unreported cost is not zero.
    costs: tuple[float, ...] = ()
    model_calls: int = 0
    retries: int = 0
    repairs: int = 0
    fallbacks: int = 0

    validations: int = 0
    validation_failures: int = 0
    #: Every overall score this scope recorded, oldest first.
    scores: tuple[float, ...] = ()
    rewrites: int = 0
    issue_statuses: tuple[FindingStatus, ...] = ()

    stalled_runs: int = 0
    interventions: int = 0
    overrides: int = 0
    surfaced_questions: int = 0
    answered_questions: int = 0
    context_items: int = 0
    truncated_context_items: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    #: The share of each article a person rewrote by hand, one entry per edit.
    edit_ratios: tuple[float, ...] = ()

    approval_gates: int = 0
    final_approvals: int = 0


def summarise(observations: RunObservations) -> RunMetrics:
    """Reduce what the rows said to the seventeen numbers, and nothing else."""
    return RunMetrics(
        project_id=observations.project_id,
        runs=observations.runs,
        stage_durations=_stage_durations(observations.stage_runs),
        token_usage=TokenTotals(
            input_tokens=observations.input_tokens, output_tokens=observations.output_tokens
        ),
        # Rounded, because a sum of floats reported to seventeen places reads as
        # precision the provider never offered.
        estimated_cost_usd=round(sum(observations.costs), 10) if observations.costs else None,
        retry_count=observations.retries,
        validation_failures=observations.validation_failures,
        schema_repair_frequency=_share(observations.repairs, observations.model_calls),
        score_change=_change(observations.scores),
        rewrite_count=observations.rewrites,
        issue_decisions=_issue_decisions(observations.issue_statuses),
        stagnation_frequency=_share(observations.stalled_runs, observations.runs),
        override_frequency=_share(observations.overrides, observations.interventions),
        question_response_rate=_share(
            observations.answered_questions, observations.surfaced_questions
        ),
        model_fallback_frequency=_share(observations.fallbacks, observations.model_calls),
        context_truncation_frequency=_share(
            observations.truncated_context_items, observations.context_items
        ),
        tool_failure_frequency=_share(observations.tool_failures, observations.tool_calls),
        human_edit_distance=_mean(observations.edit_ratios),
        final_approval_rate=_share(observations.final_approvals, observations.approval_gates),
    )


def collect_metrics(session: Session, *, project_id: str | None = None) -> RunMetrics:
    """Read the trace and report what it says about one project, or all of them."""
    return summarise(observe(session, project_id=project_id))


def observe(session: Session, *, project_id: str | None = None) -> RunObservations:
    """Gather the facts the metrics are computed from.

    Separate from :func:`collect_metrics` so a caller that wants the raw counts —
    a report, a test, a future exporter — does not have to reverse the
    arithmetic to get at them.
    """
    reader = _Reader(session, project_id)
    return reader.observations()


# ----------------------------------------------------------------------
# Gathering
# ----------------------------------------------------------------------


class _Reader:
    """Every query the surface needs, scoped once.

    The scope is applied by joining back to :class:`PipelineRun` rather than by
    each query knowing how to find a project, because that join *is* the
    definition of "in this project" for a provenance row — and a query that grew
    its own idea of it would be the way a metric ends up counting another
    project's work.
    """

    def __init__(self, session: Session, project_id: str | None) -> None:
        self._session = session
        self._project_id = project_id

    def observations(self) -> RunObservations:
        stage_runs = self._stage_runs()
        tokens, costs, calls, retries, repairs, fallbacks = self._model_calls()
        validations, failures = self._validations()
        surfaced, answered = self._questions()
        items, truncated = self._context()
        tools, tool_failures = self._tools()
        interventions, overrides = self._interventions()
        gates, approvals = self._approvals()

        return RunObservations(
            project_id=self._project_id,
            runs=self._count(select(provenance_models.PipelineRun.id)),
            stage_runs=stage_runs,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            costs=costs,
            model_calls=calls,
            retries=retries,
            repairs=repairs,
            fallbacks=fallbacks,
            validations=validations,
            validation_failures=failures,
            scores=self._scores(),
            rewrites=sum(1 for stage, _ in stage_runs if stage == REWRITE_STAGE),
            issue_statuses=self._issue_statuses(),
            stalled_runs=self._stalled_runs(),
            interventions=interventions,
            overrides=overrides,
            surfaced_questions=surfaced,
            answered_questions=answered,
            context_items=items,
            truncated_context_items=truncated,
            tool_calls=tools,
            tool_failures=tool_failures,
            edit_ratios=self._edit_ratios(),
            approval_gates=gates,
            final_approvals=approvals,
        )

    # -- scoping ------------------------------------------------------

    def _runs(self, statement: Select[Any]) -> Select[Any]:
        """Restrict a statement already joined to ``pipeline_runs``."""
        if self._project_id is None:
            return statement
        return statement.where(provenance_models.PipelineRun.project_id == self._project_id)

    def _count(self, statement: Select[Any]) -> int:
        return len(list(self._session.scalars(self._runs(statement))))

    def _executions(self) -> Select[Any]:
        return select(provenance_models.StageExecution).join(
            provenance_models.PipelineRun,
            provenance_models.PipelineRun.id == provenance_models.StageExecution.pipeline_run_id,
        )

    def _articles(self, statement: Select[Any]) -> Select[Any]:
        """Restrict a statement already joined to ``articles``."""
        if self._project_id is None:
            return statement
        return statement.where(domain_models.Article.project_id == self._project_id)

    # -- the facts ----------------------------------------------------

    def _stage_runs(self) -> tuple[tuple[str, int], ...]:
        """Every finished execution, as its stage and how long it took.

        Unfinished executions are left out rather than measured against now: a
        stage still running has no duration yet, and timing it against the clock
        would make the metric move every time it is read.
        """
        rows = self._session.scalars(
            self._runs(
                self._executions().where(provenance_models.StageExecution.completed_at.is_not(None))
            )
        )
        return tuple(
            (row.stage, _milliseconds(row))
            for row in rows
            if row.completed_at is not None and row.stage
        )

    def _model_calls(
        self,
    ) -> tuple[tuple[int, int], tuple[float, ...], int, int, int, int]:
        """Usage, cost and the three kinds of follow-up, in one pass.

        Every attempt counts, the failed ones included — the reason phase 03 put
        usage on each invocation row rather than on the accepted one.
        """
        rows = list(
            self._session.scalars(
                self._runs(
                    select(provenance_models.ModelInvocation)
                    .join(
                        provenance_models.StageExecution,
                        provenance_models.StageExecution.id
                        == provenance_models.ModelInvocation.stage_execution_id,
                    )
                    .join(
                        provenance_models.PipelineRun,
                        provenance_models.PipelineRun.id
                        == provenance_models.StageExecution.pipeline_run_id,
                    )
                )
            )
        )
        return (
            (
                sum(row.input_tokens for row in rows),
                sum(row.output_tokens for row in rows),
            ),
            tuple(row.cost_usd for row in rows if row.cost_usd is not None),
            len(rows),
            sum(1 for row in rows if row.parent_invocation_id is not None),
            sum(1 for row in rows if row.retry_type in REPAIR_RETRIES),
            sum(1 for row in rows if row.retry_type is RetryType.MODEL_FALLBACK),
        )

    def _validations(self) -> tuple[int, int]:
        rows = list(
            self._session.scalars(
                self._articles(
                    select(domain_models.ValidationReport)
                    .join(
                        domain_models.ArticleVersion,
                        domain_models.ArticleVersion.id
                        == domain_models.ValidationReport.article_version_id,
                    )
                    .join(
                        domain_models.Article,
                        domain_models.Article.id == domain_models.ArticleVersion.article_id,
                    )
                )
            )
        )
        return len(rows), sum(1 for row in rows if not row.passed)

    def _scores(self) -> tuple[float, ...]:
        """Every overall score the scoring stage recorded, oldest first.

        Restricted to the scoring stage rather than to every evaluation run: an
        evaluator that is not the rubric answers a different question, and mixing
        the two would make "score change" the distance between two things that
        were never on the same scale.
        """
        rows = self._session.scalars(
            self._runs(
                select(provenance_models.EvaluationRun)
                .join(
                    provenance_models.StageExecution,
                    provenance_models.StageExecution.id
                    == provenance_models.EvaluationRun.stage_execution_id,
                )
                .join(
                    provenance_models.PipelineRun,
                    provenance_models.PipelineRun.id
                    == provenance_models.StageExecution.pipeline_run_id,
                )
                .where(provenance_models.StageExecution.stage == SCORE_STAGE)
                .order_by(
                    provenance_models.EvaluationRun.created_at,
                    provenance_models.EvaluationRun.id,
                )
            )
        )
        return tuple(float(row.scores.get("overall", 0.0)) for row in rows)

    def _issue_statuses(self) -> tuple[FindingStatus, ...]:
        rows = self._session.scalars(
            self._articles(
                select(domain_models.ReviewIssue)
                .join(
                    domain_models.Review,
                    domain_models.Review.id == domain_models.ReviewIssue.review_id,
                )
                .join(
                    domain_models.ArticleVersion,
                    domain_models.ArticleVersion.id == domain_models.Review.article_version_id,
                )
                .join(
                    domain_models.Article,
                    domain_models.Article.id == domain_models.ArticleVersion.article_id,
                )
            )
        )
        return tuple(row.status for row in rows)

    def _stalled_runs(self) -> int:
        """How many runs the routing policy declared stalled.

        Counted per run rather than per decision: a run that stalls twice is one
        run that could not finish, and a per-decision rate would exceed one on a
        single project that went round the loop.
        """
        rows = self._session.scalars(
            self._runs(
                select(provenance_models.StageExecution.pipeline_run_id)
                .join(
                    provenance_models.DecisionRecord,
                    provenance_models.DecisionRecord.stage_execution_id
                    == provenance_models.StageExecution.id,
                )
                .join(
                    provenance_models.PipelineRun,
                    provenance_models.PipelineRun.id
                    == provenance_models.StageExecution.pipeline_run_id,
                )
                .where(provenance_models.DecisionRecord.decision_type == "stagnation")
            )
        )
        return len(set(rows))

    def _interventions(self) -> tuple[int, int]:
        rows = list(
            self._session.scalars(
                self._runs(
                    select(provenance_models.UserIntervention)
                    .join(
                        provenance_models.StageExecution,
                        provenance_models.StageExecution.id
                        == provenance_models.UserIntervention.stage_execution_id,
                    )
                    .join(
                        provenance_models.PipelineRun,
                        provenance_models.PipelineRun.id
                        == provenance_models.StageExecution.pipeline_run_id,
                    )
                )
            )
        )
        return len(rows), sum(
            1 for row in rows if row.intervention_type is InterventionType.OVERRIDE
        )

    def _questions(self) -> tuple[int, int]:
        """Surfaced questions, and how many of them a person answered.

        The denominator is what was *put to* the author, never every gap the
        model generated: phase 06 stores the suppressed ones too, and dividing by
        those would report an author ignoring questions they were never shown.
        """
        statement = select(domain_models.SourceGap).where(
            domain_models.SourceGap.surfaced.is_(True)
        )
        if self._project_id is not None:
            statement = statement.where(domain_models.SourceGap.project_id == self._project_id)
        surfaced = {row.id for row in self._session.scalars(statement)}
        if not surfaced:
            return 0, 0

        answered = {
            row
            for row in self._session.scalars(select(domain_models.UserAnswer.gap_id))
            if row in surfaced
        }
        return len(surfaced), len(answered)

    def _context(self) -> tuple[int, int]:
        rows = list(
            self._session.scalars(
                self._runs(
                    select(provenance_models.ContextItem)
                    .join(
                        provenance_models.ContextSelection,
                        provenance_models.ContextSelection.id
                        == provenance_models.ContextItem.context_selection_id,
                    )
                    .join(
                        provenance_models.StageExecution,
                        provenance_models.StageExecution.id
                        == provenance_models.ContextSelection.stage_execution_id,
                    )
                    .join(
                        provenance_models.PipelineRun,
                        provenance_models.PipelineRun.id
                        == provenance_models.StageExecution.pipeline_run_id,
                    )
                )
            )
        )
        return len(rows), sum(1 for row in rows if row.disposition is ContextDisposition.TRUNCATED)

    def _tools(self) -> tuple[int, int]:
        rows = list(
            self._session.scalars(
                self._runs(
                    select(provenance_models.ToolInvocation)
                    .join(
                        provenance_models.StageExecution,
                        provenance_models.StageExecution.id
                        == provenance_models.ToolInvocation.stage_execution_id,
                    )
                    .join(
                        provenance_models.PipelineRun,
                        provenance_models.PipelineRun.id
                        == provenance_models.StageExecution.pipeline_run_id,
                    )
                )
            )
        )
        return len(rows), sum(1 for row in rows if row.status is ExecutionStatus.FAILED)

    def _edit_ratios(self) -> tuple[float, ...]:
        """How much of each article a person rewrote by hand.

        Measured with phase 12's yardstick rather than a second one: the rubric
        signal and this metric must be looking at the same number, or an operator
        and an experiment would disagree about the same edit.
        """
        rows = self._session.scalars(
            self._articles(
                select(ManualEdit)
                .join(
                    domain_models.ArticleVersion,
                    domain_models.ArticleVersion.id == ManualEdit.article_version_id,
                )
                .join(
                    domain_models.Article,
                    domain_models.Article.id == domain_models.ArticleVersion.article_id,
                )
            )
        )
        return tuple(measure_manual_edit(row.before, row.after).character_ratio for row in rows)

    def _approvals(self) -> tuple[int, int]:
        """Runs that reached the human gate, and runs a person approved.

        Both read off the state a transition *landed in*, which the decision
        record stores as a plain column. ``completed`` is reachable only by
        ``approve_final`` and ``human_approval_required`` only by
        ``validation_passed`` (plan/05's table), so neither needs the action
        digging out of a JSON payload — and a query that avoids JSON is a query
        that behaves the same on SQLite and Postgres, which is this phase's
        other promise.
        """
        landings = Counter(
            row
            for row in self._session.scalars(
                self._runs(
                    select(provenance_models.DecisionRecord.outcome)
                    .join(
                        provenance_models.StageExecution,
                        provenance_models.StageExecution.id
                        == provenance_models.DecisionRecord.stage_execution_id,
                    )
                    .join(
                        provenance_models.PipelineRun,
                        provenance_models.PipelineRun.id
                        == provenance_models.StageExecution.pipeline_run_id,
                    )
                    .where(provenance_models.DecisionRecord.decision_type == "workflow_transition")
                )
            )
        )
        return (
            landings[WorkflowState.HUMAN_APPROVAL_REQUIRED.value],
            landings[WorkflowState.COMPLETED.value],
        )


# ----------------------------------------------------------------------
# Arithmetic
# ----------------------------------------------------------------------


def _stage_durations(stage_runs: tuple[tuple[str, int], ...]) -> tuple[StageDuration, ...]:
    """One row per stage, slowest first.

    Slowest first because that is the order the question is asked in: "the
    pipeline takes 40 seconds" cannot be acted on, "the rewrite takes 30 of them"
    can. Ties break on the stage name so the order is stable across reads.
    """
    totals: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    for stage, duration_ms in stage_runs:
        totals[stage] += duration_ms
        counts[stage] += 1
    return tuple(
        StageDuration(
            stage=stage,
            executions=counts[stage],
            total_ms=total,
            mean_ms=total / counts[stage],
        )
        for stage, total in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    )


def _issue_decisions(statuses: tuple[FindingStatus, ...]) -> IssueDecisions:
    counted = Counter(statuses)
    return IssueDecisions(
        proposed=counted[FindingStatus.PROPOSED],
        accepted=counted[FindingStatus.ACCEPTED],
        rejected=counted[FindingStatus.REJECTED],
        edited=counted[FindingStatus.EDITED],
        suppressed=counted[FindingStatus.SUPPRESSED],
    )


def _change(scores: tuple[float, ...]) -> float | None:
    """First score to last, signed. One score is not a change.

    Reporting ``0.0`` for a single score would say the revision loop ran and
    achieved nothing, which is a different claim from not having run.
    """
    if len(scores) < 2:
        return None
    return round(scores[-1] - scores[0], 10)


def _share(part: int, whole: int) -> float | None:
    """A proportion, or nothing when there was nothing to divide."""
    return part / whole if whole else None


def _mean(values: tuple[float, ...]) -> float | None:
    return fmean(values) if values else None


def _milliseconds(execution: provenance_models.StageExecution) -> int:
    assert execution.completed_at is not None
    return round((execution.completed_at - execution.started_at).total_seconds() * 1000)


__all__ = [
    "METRIC_NAMES",
    "IssueDecisions",
    "RunMetrics",
    "RunObservations",
    "StageDuration",
    "TokenTotals",
    "collect_metrics",
    "observe",
    "summarise",
]
