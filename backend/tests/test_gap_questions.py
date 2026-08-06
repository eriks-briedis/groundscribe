"""Gap analysis, the question queue, and answer provenance (phase 06 §3).

Spec (plan/06 → *GenerateGapQuestions* and its two named tests):

- prioritised gaps (blocking / high-value / optional), of which only blocking and
  *selected* high-value surface automatically, each stating why it matters;
- a queue supporting six responses — answer, skip, unknown, confidential, defer,
  "premise incorrect";
- answers that regenerate the source model with a visible diff and full answer
  provenance: the original question, its reason, the gaps addressed, the exact
  answer, its classification, the resulting diff, and the creating execution.

The risk plan/06 names for this stage is over-questioning, and prioritisation is
the mitigation — so "optional questions never surface on their own" is pinned as
hard as the six response types are.

Answers re-enter *extraction* rather than patching the stored model, which is what
the phase-05 transition table already says (`answer_questions` leads back to
`source_model_extracting`, "so the source model is rebuilt, not patched"). The
diff is what makes that rebuild reviewable instead of merely trusted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.orm import Session

from golden import golden_json, with_segment_ids
from groundscribe.domain.enums import AnswerResponse, ArtifactType, GapPriority
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.llm import FakeLLMClient
from groundscribe.provenance.enums import InterventionType
from groundscribe.stages.base import PipelineContext, StageRunner
from groundscribe.stages.extraction import ExtractSourceTruth
from groundscribe.stages.ingestion import IngestedSource
from groundscribe.stages.questions import (
    GAP_STAGE,
    GapAnalysis,
    GenerateGapQuestions,
    open_question_queue,
    surfaced_gaps,
)
from groundscribe.stages.schemas import GapReport, SourceGapQuestion, SourceModel
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.policy import (
    SourceQuestionLimits,
    WorkflowPolicy,
    default_workflow_policy,
)
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from stage_helpers import scripted_context
from test_extraction import ingest_golden, script


def policy_allowing(*, rounds: int = 1, surfaced: int = 5) -> WorkflowPolicy:
    """The shipped policy with the question limits moved.

    Named rather than inlined because two very different kinds of test want it:
    one that needs *more* questions than the cap allows to exercise something
    else, and one whose whole subject is the cap. Both should be reading the
    shipped policy and changing one field, so neither drifts from what actually
    ships.
    """
    return default_workflow_policy().model_copy(
        update={
            "source_questions": SourceQuestionLimits(
                max_rounds=rounds, max_surfaced_per_round=surfaced
            )
        }
    )


def gap(
    gap_id: str,
    priority: str,
    question: str = "What was the cold-cache p99?",
    why: str = "The headline number is warm-cache only and would overstate the win.",
) -> dict[str, Any]:
    """One gap in the shape the model returns it."""
    return {
        "id": gap_id,
        "question": question,
        "why_it_matters": why,
        "priority": priority,
        "addresses": ["c1"],
        "group": "latency numbers",
    }


GAPS: dict[str, Any] = {
    "schema_version": 1,
    "gaps": [
        gap("g1", "blocking"),
        gap(
            "g2", "high_value", "How many locales were affected?", "It sizes the incident section."
        ),
        gap("g3", "optional", "Which Markdown parser is in use?", "Colour; the argument stands."),
    ],
}

#: Six surfaced questions, one per response type the queue must accept.
SIX_GAPS: dict[str, Any] = {
    "schema_version": 1,
    "gaps": [gap(f"g{n}", "blocking", f"Question {n}?", f"Reason {n}.") for n in range(1, 7)],
}

NO_BLOCKING_GAPS: dict[str, Any] = {"schema_version": 1, "gaps": [gap("g3", "optional")]}

#: The seeded project's author. Answers are attributed to a real user row: an
#: intervention nobody can be identified as is not reviewable (plan/03).
AUTHOR = "u1"


@dataclass(frozen=True)
class Extracted:
    """What the two stages before the queue produced, for the tests that need both."""

    source: IngestedSource
    model: SourceModel
    snapshot: ArtifactSnapshot
    analysis: GapAnalysis
    gap_execution_outputs: tuple[ArtifactType, ...]


async def extract_and_analyse(
    context: PipelineContext,
    model_client: FakeLLMClient,
    payload: dict[str, Any] = GAPS,
    *,
    selected_high_value: tuple[str, ...] = (),
) -> Extracted:
    """Ingest the golden source, extract it, then generate gap questions over it."""
    source = await ingest_golden(context)
    script(model_client, with_segment_ids(golden_json("source_model.json"), source))
    extracted = await StageRunner(context).run(ExtractSourceTruth(source=source))
    model_client.script_response(GAP_STAGE, payload)
    analysed = await StageRunner(context).run(
        GenerateGapQuestions(
            source_model=extracted.value,
            selected_high_value=selected_high_value,
        )
    )
    assert analysed.execution is not None
    return Extracted(
        source=source,
        model=extracted.value,
        snapshot=extracted.outputs[0],
        analysis=analysed.value,
        gap_execution_outputs=tuple(
            artifact.snapshot.artifact_type for artifact in analysed.execution.outputs
        ),
    )


def test_only_blocking_and_selected_high_value_questions_surface() -> None:
    """plan/06 risk: over-questioning. Optional gaps never surface on their own."""
    report = GapReport.model_validate(GAPS)

    assert [g.id for g in surfaced_gaps(report, selected_high_value=())] == ["g1"]
    assert [g.id for g in surfaced_gaps(report, selected_high_value=("g2",))] == ["g1", "g2"]
    # Selecting a gap surfaces it — the author asked — but nothing optional
    # surfaces unasked.
    assert [g.id for g in surfaced_gaps(report, selected_high_value=("g3",))] == ["g1", "g3"]


def test_a_question_without_a_reason_is_refused() -> None:
    """Each question states why it matters, enforced where the model can be told."""
    with pytest.raises(ValueError, match="why it matters"):
        SourceGapQuestion(
            id="g9",
            question="What database is behind it?",
            why_it_matters="   ",
            priority=GapPriority.BLOCKING,
        )


async def test_a_blocking_gap_parks_the_run_for_the_author(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The engine parks at a human pause rather than guessing an answer."""
    context, model_client = scripted_context(db_session, snapshot_store)

    extracted = await extract_and_analyse(context, model_client)

    assert context.engine.state is WorkflowState.SOURCE_QUESTIONS_REQUIRED
    assert context.engine.is_paused
    assert [row.ref for row in extracted.analysis.surfaced] == ["g1"]


async def test_no_blocking_gap_completes_the_extraction(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Nothing blocking means the source model is ready; optional gaps do not stop it."""
    context, model_client = scripted_context(db_session, snapshot_store)

    await extract_and_analyse(context, model_client, NO_BLOCKING_GAPS)

    assert context.engine.state is WorkflowState.SOURCE_MODEL_READY


def test_a_round_puts_a_bounded_number_of_questions_on_screen() -> None:
    """The module has always named over-questioning as the risk — "an author
    faced with fifteen questions answers none" — and then capped only the
    high-value and optional gaps. Six blocking gaps meant six questions.

    Blocking gaps still come first; the cap decides how many of them arrive.
    """
    report = GapReport.model_validate(SIX_GAPS)

    surfaced = surfaced_gaps(report, limit=5)

    assert len(surfaced) == 5
    assert [item.id for item in surfaced] == [f"g{n}" for n in range(1, 6)]


def test_the_cap_takes_blocking_gaps_before_selected_ones() -> None:
    """Which five arrive is not arbitrary. A selected high-value question is one
    the author asked for, but a blocking one is what the article cannot be
    written honestly without, so it goes first when only some fit."""
    report = GapReport.model_validate(
        {
            "schema_version": 1,
            "gaps": [
                gap("h1", "high_value", "Chosen but not blocking?", "The author asked."),
                gap("b1", "blocking", "Blocking one?", "Cannot write without it."),
                gap("b2", "blocking", "Blocking two?", "Cannot write without it."),
            ],
        }
    )

    surfaced = surfaced_gaps(report, selected_high_value=("h1",), limit=2)

    assert [item.id for item in surfaced] == ["b1", "b2"]


async def test_a_second_round_of_questions_is_not_asked(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The loop this closes.

    Answers do not patch the source model — they re-enter extraction, which
    regenerates the gap report, which finds fresh blocking gaps and parks the run
    again. Nothing counted the rounds, and a real source always has something
    absent, so the cycle ended only if the model ran out of things to ask. On a
    47-claim source model it does not: the run that prompted this had been round
    three and climbing.
    """
    context, model_client = scripted_context(
        db_session, snapshot_store, policy=policy_allowing(rounds=1)
    )
    first = await extract_and_analyse(context, model_client)
    assert context.engine.state is WorkflowState.SOURCE_QUESTIONS_REQUIRED
    assert [row.ref for row in first.analysis.surfaced] == ["g1"], "round one asks"

    # The same source asked again after the answers came back, and the model
    # still finds something blocking — which it will, on any real source.
    model_client.script_response(GAP_STAGE, GAPS)
    second = await StageRunner(context).run(
        GenerateGapQuestions(source_model=first.model), transitions=False
    )

    assert second.value.surfaced == (), "round two asks nothing"
    assert second.exit_action is WorkflowAction.COMPLETE_EXTRACTION
    # The gaps are not lost by not being asked: the run proceeds knowing what it
    # does not know, which is the honest version of proceeding.
    assert len(second.value.gaps) == len(GAPS["gaps"])
    assert all(not row.surfaced for row in second.value.gaps)


async def test_a_round_that_asked_nothing_does_not_spend_the_budget(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Only rounds that surfaced something count against the cap.

    A gap analysis finding nothing blocking never put a question on screen. If
    that consumed the single allowed round, a clean first pass would silently
    spend the round a later one — after re-extraction, over a bigger source
    model — actually needed.
    """
    context, model_client = scripted_context(
        db_session, snapshot_store, policy=policy_allowing(rounds=1)
    )
    first = await extract_and_analyse(context, model_client, NO_BLOCKING_GAPS)
    assert first.analysis.surfaced == (), "nothing was asked"

    model_client.script_response(GAP_STAGE, GAPS)
    second = await StageRunner(context).run(
        GenerateGapQuestions(source_model=first.model), transitions=False
    )

    assert [row.ref for row in second.value.surfaced] == ["g1"], "the round was still available"


async def test_gaps_are_persisted_with_their_priority_question_and_reason(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Every gap is stored, not only the surfaced ones: suppression is a decision."""
    context, model_client = scripted_context(db_session, snapshot_store)

    extracted = await extract_and_analyse(context, model_client)
    rows = extracted.analysis.gaps

    assert len(rows) == 3
    assert {row.priority for row in rows} == {
        GapPriority.BLOCKING,
        GapPriority.HIGH_VALUE,
        GapPriority.OPTIONAL,
    }
    assert all(row.question and row.why_it_matters for row in rows)
    assert all(row.created_by_execution_id for row in rows)
    assert extracted.gap_execution_outputs == (ArtifactType.SOURCE_GAP_REPORT,)


async def test_the_queue_offers_the_surfaced_questions_and_takes_all_six_responses(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 → answer / skip / unknown / confidential / defer / premise incorrect.

    Five of the six close the question. ``DEFERRED`` deliberately does not: a
    postponed question is still pending, and marking it resolved would lose the
    only record that the author meant to come back to it.

    Runs with the per-round cap raised, because the subject here is the six
    *responses* and it needs six questions on screen to exercise one of each.
    Under the shipped cap of five this test would silently be testing five of
    them — which is exactly what a cap is for, and exactly not what this asserts.
    """
    context, model_client = scripted_context(
        db_session, snapshot_store, policy=policy_allowing(surfaced=len(AnswerResponse))
    )
    await extract_and_analyse(context, model_client, SIX_GAPS)

    queue = open_question_queue(context)
    assert [row.ref for row in queue.pending] == [f"g{n}" for n in range(1, 7)]

    # ``strict`` is the assertion that there are exactly six response types: one
    # question each, no more and no fewer.
    responses = list(AnswerResponse)
    for row, response in zip(queue.pending, responses, strict=True):
        answer = queue.respond(row, response=response, text="12ms", answered_by=AUTHOR)
        assert answer.response_type is response

    still_open = f"g{responses.index(AnswerResponse.DEFERRED) + 1}"
    assert [row.ref for row in queue.pending] == [still_open]


async def test_an_answer_retains_its_question_reason_gaps_text_and_execution(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 answer-provenance test, minus the diff (which re-extraction adds)."""
    context, model_client = scripted_context(db_session, snapshot_store)
    extracted = await extract_and_analyse(context, model_client)
    asked = extracted.analysis.surfaced[0]

    queue = open_question_queue(context)
    answer = queue.respond(
        queue.pending[0],
        response=AnswerResponse.ANSWERED,
        text="Cold cache p99 was 690ms over the same seven-day window.",
        answered_by=AUTHOR,
    )

    assert answer.question == asked.question
    assert answer.why_it_matters == asked.why_it_matters
    assert answer.text == "Cold cache p99 was 690ms over the same seven-day window."
    assert answer.response_type is AnswerResponse.ANSWERED
    assert answer.answered_by == AUTHOR
    assert [addressed.id for addressed in answer.gaps] == [asked.id]
    assert answer.created_by_execution_id == queue.execution.id
    # A human control point, recorded as one (plan/03 → user interventions).
    (intervention,) = queue.execution.user_interventions
    assert intervention.intervention_type is InterventionType.ANSWER


async def test_only_sendable_answers_reach_the_model(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Confidential material is recorded and withheld; a skip has nothing to send.

    The author saying "this is confidential" is an answer to the pipeline and a
    refusal to the provider; conflating the two would send exactly the material the
    flag exists to keep local.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    await extract_and_analyse(context, model_client, SIX_GAPS)

    queue = open_question_queue(context)
    queue.respond(
        queue.pending[0],
        response=AnswerResponse.CONFIDENTIAL,
        text="Northwind's contract forbids naming them.",
        answered_by=AUTHOR,
    )
    queue.respond(queue.pending[0], response=AnswerResponse.SKIPPED, text="", answered_by=AUTHOR)
    queue.respond(
        queue.pending[0],
        response=AnswerResponse.UNKNOWN,
        text="never measured it",
        answered_by=AUTHOR,
    )
    queue.respond(
        queue.pending[0],
        response=AnswerResponse.PREMISE_INCORRECT,
        text="The parser was never the bottleneck; the AST walk was.",
        answered_by=AUTHOR,
    )
    queue.respond(
        queue.pending[0],
        response=AnswerResponse.ANSWERED,
        text="Cold cache p99 was 690ms.",
        answered_by=AUTHOR,
    )

    sendable = queue.sendable_answers()
    assert [answer.response_type for answer in sendable] == [
        AnswerResponse.PREMISE_INCORRECT,
        AnswerResponse.ANSWERED,
    ]
    assert all("Northwind" not in answer.text for answer in sendable)


async def test_answers_rebuild_the_source_model_with_a_visible_linked_diff(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/05 table: answers re-enter extraction so the model is rebuilt, not patched."""
    context, model_client = scripted_context(db_session, snapshot_store)
    extracted = await extract_and_analyse(context, model_client)

    queue = open_question_queue(context)
    answer = queue.respond(
        queue.pending[0],
        response=AnswerResponse.ANSWERED,
        text="Cold cache p99 was 690ms.",
        answered_by=AUTHOR,
    )
    queue.submit(submitted_by=AUTHOR)
    assert context.engine.state is WorkflowState.SOURCE_MODEL_EXTRACTING

    amended = with_segment_ids(golden_json("source_model.json"), extracted.source)
    amended["claims"][0]["text"] = "Warm-cache p99 fell from 810ms to 120ms; cold cache was 690ms."
    script(model_client, amended)
    rebuilt = await StageRunner(context).run(
        ExtractSourceTruth(
            source=extracted.source,
            entry_action=None,
            previous=extracted.model,
            previous_snapshot=extracted.snapshot,
            answers=(answer,),
        )
    )

    assert rebuilt.value != extracted.model
    (stored_diff,) = [s for s in rebuilt.outputs if s.artifact_type is ArtifactType.STRUCTURED_DIFF]
    entries = json.loads(snapshot_store.read(stored_diff).decode("utf-8"))["entries"]
    assert any(entry["path"] == "claims.0.text" for entry in entries)
    assert answer.diff_snapshot_id == stored_diff.id
    # The answer's text reached the prompt; it is what the rebuild is for.
    sent = model_client.last_request
    assert sent is not None
    assert "690ms" in sent.prompt


async def test_the_rebuilt_model_supersedes_its_parent_rather_than_replacing_it(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/00 → no silent mutation; the earlier source model stays readable."""
    context, model_client = scripted_context(db_session, snapshot_store)
    extracted = await extract_and_analyse(context, model_client)

    queue = open_question_queue(context)
    answer = queue.respond(
        queue.pending[0], response=AnswerResponse.ANSWERED, text="690ms", answered_by=AUTHOR
    )
    queue.submit(submitted_by=AUTHOR)
    script(model_client, with_segment_ids(golden_json("source_model.json"), extracted.source))
    rebuilt = await StageRunner(context).run(
        ExtractSourceTruth(
            source=extracted.source,
            entry_action=None,
            previous=extracted.model,
            previous_snapshot=extracted.snapshot,
            answers=(answer,),
        )
    )

    (model_snapshot,) = [s for s in rebuilt.outputs if s.artifact_type is ArtifactType.SOURCE_MODEL]
    assert model_snapshot.parent_snapshot_id == extracted.snapshot.id
    assert snapshot_store.verify(extracted.snapshot) is True


async def test_an_unattributed_answer_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/03: an intervention nobody can be identified as is unreviewable."""
    context, model_client = scripted_context(db_session, snapshot_store)
    await extract_and_analyse(context, model_client)
    queue = open_question_queue(context)

    with pytest.raises(ValueError, match="answered_by"):
        queue.respond(
            queue.pending[0], response=AnswerResponse.ANSWERED, text="690ms", answered_by=""
        )


async def test_a_second_round_may_reuse_a_label_without_colliding(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The model names its questions; two rounds may name them the same thing.

    Nothing obliges a model to invent fresh labels for a source it is reading
    again — re-extraction after an answer, or phase 12's replay, asks the same
    question of the same document and tends to get ``g1`` back. Keying the row on
    that label made the second round a primary-key collision, which surfaced as
    an ``IntegrityError`` from a job rather than as anything a person could act
    on.

    The row keeps its own id and remembers the label as ``ref``, which is what
    phase 07 already does for review findings: a reviewer renumbers from one
    every round, and two rounds of findings have to coexist.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    first = await extract_and_analyse(context, model_client)

    # The same source, asked again — a rebuild after an answer, or a phase-12
    # replay — and the model hands back the labels it used last time.
    model_client.script_response(GAP_STAGE, GAPS)
    second = await StageRunner(context).run(
        GenerateGapQuestions(source_model=first.model), transitions=False
    )

    labels = [row.ref for row in first.analysis.gaps] + [row.ref for row in second.value.gaps]
    ids = [row.id for row in first.analysis.gaps] + [row.id for row in second.value.gaps]
    assert labels.count("g1") == 2, "the same question, asked twice"
    assert len(set(ids)) == len(ids), "and two rows, not one row written over"


# ----------------------------------------------------------------------
# Changing your mind before the round is handed back
# ----------------------------------------------------------------------


async def test_answering_again_revises_rather_than_adding_a_second_answer(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """An author works down a queue and changes their mind halfway.

    Both answers would otherwise reach the rebuild, which folds every answer in
    as source truth of the same standing as the document — so the model would be
    handed a contradiction with no way to tell which the author meant.

    Revised in place rather than superseded: until the rebuild reads it, an
    answer is not yet a fact about the run, so there is no history to keep.
    """
    from sqlalchemy import select

    from groundscribe.domain import models as domain_models

    context, model_client = scripted_context(db_session, snapshot_store)
    await extract_and_analyse(context, model_client)
    queue = open_question_queue(context)
    gap = queue.pending[0]

    queue.respond(gap, response=AnswerResponse.ANSWERED, text="first", answered_by=AUTHOR)
    queue.respond(gap, response=AnswerResponse.ANSWERED, text="second", answered_by=AUTHOR)

    stored = db_session.scalars(
        select(domain_models.UserAnswer).where(domain_models.UserAnswer.gap_id == gap.id)
    ).all()
    assert [answer.text for answer in stored] == ["second"]


async def test_an_answer_a_rebuild_has_read_is_not_revised_in_place(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Once extraction has folded it in, the source model was built from it.

    Editing it then would describe a rebuild that never happened, so a further
    answer is a new one, which the next rebuild folds in beside it.
    """
    from sqlalchemy import select

    from groundscribe.domain import models as domain_models

    context, model_client = scripted_context(db_session, snapshot_store)
    await extract_and_analyse(context, model_client)
    queue = open_question_queue(context)
    gap = queue.pending[0]

    first = queue.respond(gap, response=AnswerResponse.ANSWERED, text="first", answered_by=AUTHOR)
    # A real snapshot, because the column is a foreign key: this is the shape
    # extraction leaves behind when it folds an answer into a rebuild.
    snapshot = db_session.scalars(select(ArtifactSnapshot)).first()
    assert snapshot is not None
    first.diff_snapshot_id = snapshot.id
    db_session.flush()

    queue.respond(gap, response=AnswerResponse.ANSWERED, text="second", answered_by=AUTHOR)

    stored = db_session.scalars(
        select(domain_models.UserAnswer).where(domain_models.UserAnswer.gap_id == gap.id)
    ).all()
    assert sorted(answer.text for answer in stored) == ["first", "second"]


async def test_withdrawing_an_answer_reopens_the_question(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Deferring one already answered has to un-close it.

    Otherwise the queue shows settled a question the author has just withdrawn,
    and the rebuild proceeds as though it had been answered.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    await extract_and_analyse(context, model_client)
    queue = open_question_queue(context)
    gap = queue.pending[0]

    queue.respond(gap, response=AnswerResponse.ANSWERED, text="yes", answered_by=AUTHOR)
    assert gap.resolved is True

    queue.respond(gap, response=AnswerResponse.DEFERRED, text="", answered_by=AUTHOR)

    assert gap.resolved is False
