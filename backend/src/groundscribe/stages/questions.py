"""Gap analysis and the author's question queue (phase 06 §3).

plan/06 → *GenerateGapQuestions*: prioritised gaps of which only blocking and
*selected* high-value surface, each stating why it matters; a queue supporting six
responses; answers that rebuild the source model with a visible diff and full
provenance.

Three decisions shape this module.

**Prioritisation is a suppression policy, and suppression is recorded.** The risk
plan/06 names here is over-questioning: an author faced with fifteen questions
answers none. So every gap is stored — including the ones nobody was shown — with
the ``surfaced`` flag saying which the policy offered. A gap that was suppressed
and a gap that was never generated look identical otherwise, and only one of those
is a bug.

**The gap stage decides where the run goes next.** Extraction cannot: whether the
source model is ready depends on whether anything blocking is missing, which is
what this stage computes. So it returns its exit edge with its result rather than
declaring one up front.

**Answers do not patch the source model.** They re-enter extraction, which is what
the phase-05 transition table already says. A patched model would be a model no
prompt ever produced, and the provenance chain — this text, from this prompt, from
this source — would have a hole in it exactly where a human edit went in.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy import select

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import AnswerResponse, ArtifactType, GapPriority
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType, InterventionType
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.extraction import require_permitted_provider
from groundscribe.stages.schemas import GapReport, SourceGapQuestion, SourceModel
from groundscribe.workflow.states import WorkflowAction

#: The stage name, routing key and prompt template id (see extraction for why
#: these are deliberately one name).
GAP_STAGE = "generate_gap_questions"

#: The stage under which a person's answers are recorded.
ANSWER_STAGE = "answer_source_questions"


def _rounds_already_asked(context: PipelineContext, execution: models.StageExecution) -> int:
    """How many times this run has already put questions to the author.

    Counted from the executions the run already recorded rather than kept in a
    column, for the reason phase 15 gave about routing profiles: two records of
    one fact eventually disagree, and the one a person is shown is not
    necessarily the one that decided anything. The executions are the fact.

    Counted from the *gaps*, not from the executions that made them, because
    only a round that surfaced something actually asked the author anything. A
    gap analysis that found nothing blocking never put a question on screen, and
    charging it against the budget would let a clean first pass silently consume
    the only round a later one needed. A surfaced gap row names the execution
    that surfaced it, so "how many rounds asked" is a count of distinct such
    executions and needs no counter of its own.
    """
    asked = context.session.scalars(
        select(domain_models.SourceGap.created_by_execution_id)
        .where(
            domain_models.SourceGap.project_id == context.project_id,
            domain_models.SourceGap.surfaced.is_(True),
            domain_models.SourceGap.created_by_execution_id.is_not(None),
            domain_models.SourceGap.created_by_execution_id != execution.id,
        )
        .distinct()
    )
    return len(set(asked))


def surfaced_gaps(
    report: GapReport,
    *,
    selected_high_value: Collection[str] = (),
    limit: int | None = None,
) -> tuple[SourceGapQuestion, ...]:
    """The questions to put to the author, and only those.

    Blocking gaps qualify first: the article cannot be written honestly without
    them. Everything else qualifies only if the author selected it — including
    optional gaps, because a question the author asked for is a question they
    want, whatever the model graded it.

    ``limit`` then caps what actually reaches them, blocking gaps first. The
    module docstring above has always named over-questioning as the risk here,
    but the suppression policy only covered high-value and optional gaps, so a
    report with fifteen blocking gaps put fifteen questions on the screen and got
    none of them answered. A cap is what makes "blocking gaps always surface"
    survivable — the rest are still stored, still unresolved, and still say what
    the run does not know.
    """
    chosen = set(selected_high_value)
    qualifying = [
        gap for gap in report.gaps if gap.priority is GapPriority.BLOCKING or gap.id in chosen
    ]
    # Blocking first, and otherwise in the order the model produced them: it
    # ordered them itself, and re-sorting on anything else would be this function
    # inventing a priority the report does not carry.
    qualifying.sort(key=lambda gap: gap.priority is not GapPriority.BLOCKING)
    return tuple(qualifying if limit is None else qualifying[:limit])


@dataclass(frozen=True)
class GapAnalysis:
    """What gap analysis produced: the report, the stored gaps, and what surfaced."""

    report: GapReport
    gaps: tuple[domain_models.SourceGap, ...]
    surfaced: tuple[domain_models.SourceGap, ...]

    @property
    def blocking(self) -> bool:
        """Whether anything surfaced needs answering before the model is usable."""
        return bool(self.surfaced)


class GenerateGapQuestions:
    """Ask what the source does not say, and decide whether a person must answer.

    Takes no entry edge — it runs while the machine is already in
    ``SOURCE_MODEL_EXTRACTING`` — and chooses its exit edge from what it finds:
    ``REQUEST_ANSWERS`` parks the run for the author, ``COMPLETE_EXTRACTION``
    declares the source model ready.
    """

    name: ClassVar[str] = GAP_STAGE
    impl_version: ClassVar[str] = "1.0"
    entry_action: ClassVar[WorkflowAction | None] = None
    exit_action: ClassVar[WorkflowAction | None] = None

    def __init__(
        self,
        *,
        source_model: SourceModel,
        selected_high_value: Sequence[str] = (),
        template_version: str | None = None,
        override: RouteOverride | None = None,
    ) -> None:
        self._source_model = source_model
        self._selected = tuple(selected_high_value)
        self._template_version = template_version
        self._override = override

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[GapAnalysis]:
        """Generate the gaps, store them all, and surface the ones that qualify."""
        require_permitted_provider(context, GAP_STAGE, override=self._override)

        generated = await context.generator.generate(
            execution,
            stage=GAP_STAGE,
            template_id=GAP_STAGE,
            template_version=self._template_version,
            variables={
                "source_model": self._source_model.model_dump(mode="json"),
                "audience": context.constraints.audience,
                "depth": context.constraints.depth.value,
            },
            schema=GapReport,
            override=self._override,
        )
        report = generated.value
        limits = context.engine.policy.source_questions
        rounds_taken = _rounds_already_asked(context, execution)
        may_ask = rounds_taken < limits.max_rounds

        # Nothing surfaces once the rounds are spent. Surfacing questions the run
        # will not wait for would put a queue on screen that answering cannot
        # affect, which is worse than the silence: the gaps are still stored, and
        # still unresolved, so what is missing stays visible where it is true.
        surfaced = (
            {
                gap.id
                for gap in surfaced_gaps(
                    report,
                    selected_high_value=self._selected,
                    limit=limits.max_surfaced_per_round,
                )
            }
            if may_ask
            else set()
        )
        rows = self._store(context, execution, report, surfaced)

        context.recorder.record_decision(
            execution,
            decision_type="gap_prioritisation",
            decided_by=GAP_STAGE,
            decided_by_type=ActorType.POLICY,
            policy_version=self.impl_version,
            inputs={
                "gaps": [{"id": gap.id, "priority": gap.priority.value} for gap in report.gaps],
                "selected_high_value": list(self._selected),
                "rounds_already_asked": rounds_taken,
                "max_rounds": limits.max_rounds,
                "max_surfaced_per_round": limits.max_surfaced_per_round,
            },
            outcome=f"{len(surfaced)} of {len(report.gaps)} surfaced",
            rationale=(
                (
                    "blocking gaps surface first, capped per round because "
                    "over-questioning is what stops answers coming; high-value and "
                    "optional ones only when the author selected them"
                )
                if may_ask
                else (
                    f"the question rounds are spent ({rounds_taken} of "
                    f"{limits.max_rounds}), so extraction completes on what is known "
                    "and the remaining gaps stay recorded as unresolved rather than "
                    "parking the run again"
                )
            ),
        )
        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.SOURCE_GAP_REPORT,
            content=report.model_dump(mode="json"),
            role="gap_report",
        )
        analysis = GapAnalysis(
            report=report,
            gaps=rows,
            surfaced=tuple(row for row in rows if row.surfaced),
        )
        return StageResult(
            value=analysis,
            outputs=(snapshot,),
            invocations=generated.attempts,
            usage=generated.usage,
            # Where the run goes next is this stage's finding, not its declaration:
            # a surfaced gap parks it for the author, nothing surfaced completes
            # it. With the rounds spent nothing surfaces, so the second branch is
            # also what ends the loop — the cap is expressed as "stop asking",
            # not as a separate exit, because a run that asked its last question
            # and a run that had none left to ask are in the same position.
            exit_action=(
                WorkflowAction.REQUEST_ANSWERS
                if analysis.blocking
                else WorkflowAction.COMPLETE_EXTRACTION
            ),
            detail={
                "gaps": len(rows),
                "surfaced": len(analysis.surfaced),
                "round": rounds_taken + 1,
                "rounds_allowed": limits.max_rounds,
            },
        )

    def _store(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        report: GapReport,
        surfaced: Collection[str],
    ) -> tuple[domain_models.SourceGap, ...]:
        """Persist every gap, surfaced or not, in the order the model produced them."""
        rows = tuple(
            domain_models.SourceGap(
                id=uuid.uuid4().hex,
                ref=gap.id,
                project_id=context.project_id,
                description=gap.question,
                question=gap.question,
                why_it_matters=gap.why_it_matters,
                priority=gap.priority,
                group=gap.group,
                ordinal=ordinal,
                surfaced=gap.id in surfaced,
                created_by_execution_id=execution.id,
            )
            for ordinal, gap in enumerate(report.gaps)
        )
        context.session.add_all(rows)
        context.session.flush()
        return rows


class QuestionQueue:
    """The author's side of gap analysis: what is being asked, and what they said.

    Every response is recorded, including the ones that decline to answer. "The
    author skipped this" and "nobody has looked at this yet" are different states
    of the same question, and only one of them means the pipeline is still waiting.
    """

    def __init__(self, context: PipelineContext, execution: models.StageExecution) -> None:
        self._context = context
        self._execution = execution

    @property
    def execution(self) -> models.StageExecution:
        """The execution these answers are recorded against."""
        return self._execution

    @property
    def pending(self) -> tuple[domain_models.SourceGap, ...]:
        """The surfaced questions still waiting on the author, in order."""
        stmt = (
            select(domain_models.SourceGap)
            .where(
                domain_models.SourceGap.project_id == self._context.project_id,
                domain_models.SourceGap.surfaced.is_(True),
                domain_models.SourceGap.resolved.is_(False),
            )
            .order_by(domain_models.SourceGap.ordinal)
        )
        return tuple(self._context.session.execute(stmt).scalars())

    @property
    def answers(self) -> tuple[domain_models.UserAnswer, ...]:
        """Every answer recorded through this queue, in the order asked.

        Ordered by the *question's* position rather than by insertion. The queue
        presents questions in a deliberate order, and an author who answers the
        third one first has not changed which question came third — reordering the
        record to match their typing would make two runs of the same interview
        look different.
        """
        stmt = (
            select(domain_models.UserAnswer)
            .join(
                domain_models.SourceGap,
                domain_models.UserAnswer.gap_id == domain_models.SourceGap.id,
            )
            .where(domain_models.UserAnswer.created_by_execution_id == self._execution.id)
            .order_by(domain_models.SourceGap.ordinal)
        )
        return tuple(self._context.session.execute(stmt).scalars())

    def sendable_answers(self) -> tuple[domain_models.UserAnswer, ...]:
        """The answers whose text may be put in a prompt.

        Confidential answers are recorded and withheld: the author saying "this is
        confidential" is an answer to the pipeline and a refusal to the provider at
        the same time, and conflating the two would send exactly the material the
        flag exists to keep local.
        """
        return tuple(answer for answer in self.answers if answer.response_type.may_be_sent)

    def respond(
        self,
        gap: domain_models.SourceGap,
        *,
        response: AnswerResponse,
        text: str = "",
        answered_by: str,
        also_closes: Sequence[domain_models.SourceGap] = (),
    ) -> domain_models.UserAnswer:
        """Record one response, closing the question unless it was deferred."""
        if not answered_by:
            raise ValueError("answered_by is required: an unattributed answer is unreviewable")

        closed = [gap, *also_closes] if response.closes_the_gap else []
        answer = domain_models.UserAnswer(
            id=uuid.uuid4().hex,
            gap_id=gap.id,
            text=text,
            # Copied, not referenced: a later round may re-word the question, and
            # an answer that re-pointed at the new wording would misrepresent what
            # the author was asked.
            question=gap.question,
            why_it_matters=gap.why_it_matters,
            response_type=response,
            answered_by=answered_by,
            created_by_execution_id=self._execution.id,
            gaps=list(closed),
        )
        for closing in closed:
            closing.resolved = True
        self._context.session.add(answer)
        self._context.session.flush()

        self._context.recorder.record_user_intervention(
            self._execution,
            user_id=answered_by,
            intervention_type=InterventionType.ANSWER,
            payload={
                "gap_id": gap.id,
                "response_type": response.value,
                "closes": [row.id for row in closed],
                # The text itself goes through the recorder's redaction on the way
                # in; a confidential answer is recorded, not hidden.
                "answer": text,
            },
        )
        return answer

    def submit(self, *, submitted_by: str) -> None:
        """Hand the answers back to the pipeline, moving the run out of the pause.

        A user edge, taken by the person who submitted: the engine cannot leave a
        review state on its own, and attributing the move to the pipeline would
        make the one transition a human definitely made look automatic.
        """
        self._context.engine.apply(
            WorkflowAction.ANSWER_QUESTIONS,
            actor_id=submitted_by,
            actor_type=ActorType.USER,
            rationale="the author answered the surfaced questions",
        )


def open_question_queue(context: PipelineContext) -> QuestionQueue:
    """Open a question queue with its own stage execution to record against."""
    execution = context.engine.begin_stage(ANSWER_STAGE, impl_version="1.0")
    return QuestionQueue(context, execution)


__all__ = [
    "ANSWER_STAGE",
    "GAP_STAGE",
    "GapAnalysis",
    "GenerateGapQuestions",
    "QuestionQueue",
    "open_question_queue",
    "surfaced_gaps",
]
