"""The pipeline as a person follows it (phase 11's interface, phase 05's machine).

plan/11 → the frontend *displays backend state and submits commands; it never
re-implements pipeline-transition rules*. A progress indicator is where that rule
is easiest to break: "which phase is this state in, and how many are left" is
knowledge about the machine, and a copy of it in a screen would be a second
opinion that drifts the first time a state is added.

So the phases are published, and these tests are about the two ways that
publication could quietly become a lie:

- **Coverage.** Every state belongs to exactly one phase, or is an ending. A
  state nobody mapped would leave a run showing no progress at all, which is the
  failure that looks most like the application being broken.
- **Agreement.** Who the run is waiting on is the transition table's answer
  (``is_human_pause``), not a list of state names maintained beside it.
"""

from __future__ import annotations

import pytest

from groundscribe.workflow.journey import (
    ENDINGS,
    PHASES,
    Progress,
    headline_for,
    STATE_HEADLINES,
    journey_of,
    phase_of,
    waiting_on,
)
from groundscribe.workflow.states import WorkflowState
from groundscribe.workflow.transitions import human_pause_states

S = WorkflowState


@pytest.mark.parametrize("state", list(WorkflowState))
def test_every_state_is_either_in_one_phase_or_is_an_ending(state: WorkflowState) -> None:
    """No state falls through: a run always knows where it is on the strip."""
    phases = [phase for phase in PHASES if state in phase.states]

    assert len(phases) == 1 or (not phases and state in ENDINGS)


@pytest.mark.parametrize("state", list(WorkflowState))
def test_every_state_says_what_is_happening_in_words(state: WorkflowState) -> None:
    """An interface should never have to show ``substantive_rewriting`` to a person."""
    assert STATE_HEADLINES[state]
    assert "_" not in STATE_HEADLINES[state]


def test_the_phases_are_the_pipeline_in_order() -> None:
    """The order is the product's, and it is the order plan/00 describes."""
    assert [phase.id for phase in PHASES] == [
        "source",
        "architecture",
        "brief",
        "draft",
        "review",
        "voice",
        "score",
        "publish",
    ]


def test_a_run_in_flight_marks_what_is_done_what_is_now_and_what_is_left() -> None:
    """The three states a step can be in, which is all a strip has to say."""
    steps = journey_of(S.BRIEF_REVIEW_REQUIRED)

    assert [step.status for step in steps] == [
        "done",
        "done",
        "current",
        "upcoming",
        "upcoming",
        "upcoming",
        "upcoming",
        "upcoming",
    ]


def test_a_finished_run_has_no_step_left_undone() -> None:
    """Completion is not "the last phase is current"; it is every phase behind you."""
    steps = journey_of(S.COMPLETED)

    assert {step.status for step in steps} == {"done"}


def test_a_stopped_run_claims_no_progress_it_cannot_prove() -> None:
    """A cancelled run's state says nothing about how far it got, so neither does this.

    The honest projection is "not current, not claimed done": the dashboard says
    the run was stopped, and the strip does not invent a high-water mark from a
    state that does not have one.
    """
    steps = journey_of(S.CANCELLED)

    assert {step.status for step in steps} == {"upcoming"}


@pytest.mark.parametrize("state", sorted(human_pause_states()))
def test_a_pause_says_it_is_waiting_on_you(state: WorkflowState) -> None:
    """The same predicate the engine parks on, so the two cannot disagree."""
    assert waiting_on(state) == "you"


@pytest.mark.parametrize(
    "state", [S.SOURCE_MODEL_EXTRACTING, S.DRAFT_GENERATING, S.SCORING, S.FINAL_VALIDATING]
)
def test_work_in_flight_says_it_is_waiting_on_the_pipeline(state: WorkflowState) -> None:
    assert waiting_on(state) == "pipeline"


@pytest.mark.parametrize("state", sorted(ENDINGS))
def test_an_ended_run_waits_on_nobody(state: WorkflowState) -> None:
    """Including the successful ending: "waiting" is the wrong word for finished."""
    assert waiting_on(state) == "nobody"


def test_the_phase_a_state_belongs_to_is_answerable_on_its_own() -> None:
    """Used to link a screen to the phase it serves, so it is worth its own function."""
    assert phase_of(S.SOURCE_QUESTIONS_REQUIRED) is not None
    assert phase_of(S.SOURCE_QUESTIONS_REQUIRED).id == "source"  # type: ignore[union-attr]
    assert phase_of(S.CANCELLED) is None


# ----------------------------------------------------------------------
# States that mean two things
# ----------------------------------------------------------------------


def test_the_plan_state_asks_for_the_thing_it_is_actually_waiting_for() -> None:
    """``REVISION_PLAN_REQUIRED`` covers a review landing *and* a plan waiting.

    One state, two unrelated requests: triage the findings, or approve the plan
    those decisions produced. The stored headline describes the second, and was
    shown for both — so an author with nine findings in front of them was told
    to approve a plan that could not exist until they had decided all nine.
    """
    triage = headline_for(S.REVISION_PLAN_REQUIRED, Progress(findings_undecided=True))
    planning = headline_for(S.REVISION_PLAN_REQUIRED, Progress())
    approval = headline_for(S.REVISION_PLAN_REQUIRED, Progress(revision_plan_ready=True))

    assert "findings" in triage
    assert "approve the plan" not in triage
    assert "Planning" in planning
    assert approval == STATE_HEADLINES[S.REVISION_PLAN_REQUIRED]


@pytest.mark.parametrize(
    "state",
    [state for state in S if state is not S.REVISION_PLAN_REQUIRED],
)
def test_every_other_state_keeps_the_line_it_declares(state: WorkflowState) -> None:
    """The refinement is an exception, not a second table to keep in step."""
    assert headline_for(state, Progress(findings_undecided=True)) == STATE_HEADLINES[state]


def test_a_state_the_pipeline_will_never_leave_says_it_is_waiting_on_you() -> None:
    """``route_revision`` is actored ``policy``, so ``is_human_pause`` says "pipeline".

    Nothing runs it. ``REVISION_REQUIRED`` is one of ``advance.HUMAN_GATES``, so
    no worker picks it up and the run sits until a person presses something. The
    edge's actor describes who *chooses the destination*, not who starts it.
    """
    assert waiting_on(S.REVISION_REQUIRED) == "you"
    assert "Your turn" in STATE_HEADLINES[S.REVISION_REQUIRED]
