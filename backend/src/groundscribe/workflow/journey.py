"""The pipeline as a person follows it (phase 05's machine, phase 11's interface).

The state machine is written for the engine: twenty-three states, because that is
how many distinct situations the pipeline can be in. Nobody follows their own work
in twenty-three steps, and ``substantive_rewriting`` is not a sentence.

So this module says the same thing at the size a person reads: eight phases, each
covering the states that belong to it, and one line of English per state saying
what is happening. It adds no rule — every phase is a *grouping* of states the
transition table already defines, and who a run is waiting on is
:func:`~groundscribe.workflow.transitions.is_human_pause`'s answer, asked here
rather than restated.

It lives beside the machine rather than in the API for the reason plan/11 gives:
a progress strip is the easiest place for an interface to grow a second opinion
of the workflow. Published from here, there is nothing for a screen to know.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from groundscribe.workflow.states import WorkflowState as S
from groundscribe.workflow.transitions import TERMINAL_STATES, is_human_pause

#: Where a run can end up that is not a phase of the work.
#:
#: ``COMPLETED`` is here too: finishing is not a ninth thing to do, it is the
#: state of having done the eight.
ENDINGS: frozenset[S] = frozenset({S.COMPLETED, S.FAILED, S.CANCELLED})


@dataclass(frozen=True)
class Phase:
    """One phase of the work, and the machine states it covers.

    ``blurb`` is what the phase is *for*, in the author's terms rather than the
    pipeline's — it is the sentence that makes a strip of eight labels legible to
    someone who has not read plan/05.
    """

    id: str
    title: str
    blurb: str
    states: tuple[S, ...]


@dataclass(frozen=True)
class Step:
    """One phase, seen from a particular position in the run."""

    id: str
    title: str
    blurb: str
    #: ``"done"``, ``"current"`` or ``"upcoming"``.
    status: str


#: The pipeline, in order, covering every state that is not an ending.
#:
#: The grouping follows what the author is *doing*, which is why review keeps the
#: rewrite states: planning a revision, rewriting under the plan and reviewing
#: the result are one activity that loops, and a strip that advanced and went
#: back would report a loop as a regression.
PHASES: tuple[Phase, ...] = (
    Phase(
        id="source",
        title="Source",
        blurb="Turn the material into checked facts, and ask what it does not say.",
        states=(
            S.SOURCE_INGESTED,
            S.SOURCE_MODEL_EXTRACTING,
            S.SOURCE_QUESTIONS_REQUIRED,
            S.SOURCE_MODEL_READY,
        ),
    ),
    Phase(
        id="architecture",
        title="Architecture",
        blurb="Decide what to write: one article or a series, and what each argues.",
        states=(
            S.ARCHITECTURE_PROPOSING,
            S.ARCHITECTURE_REVIEW_REQUIRED,
            S.ARCHITECTURE_APPROVED,
        ),
    ),
    Phase(
        id="brief",
        title="Brief",
        blurb="Fix the angle, the audience and the length before a word is drafted.",
        states=(S.BRIEF_GENERATING, S.BRIEF_REVIEW_REQUIRED),
    ),
    Phase(
        id="draft",
        title="Draft",
        blurb="Write the first version against the brief and the source model.",
        states=(S.DRAFT_GENERATING,),
    ),
    Phase(
        id="review",
        title="Review",
        blurb="Find what is wrong with the argument, plan the fix, and rewrite.",
        states=(
            S.SUBSTANTIVE_REVIEWING,
            S.REVISION_PLAN_REQUIRED,
            S.SUBSTANTIVE_REWRITING,
        ),
    ),
    Phase(
        id="voice",
        title="Voice",
        blurb="Make it sound like you, without changing what it claims.",
        states=(S.VOICE_ALIGNING,),
    ),
    Phase(
        id="score",
        title="Score",
        blurb="Grade it against the rubric, and route anything that falls short.",
        states=(S.SCORING, S.REVISION_REQUIRED, S.PASSED, S.STALLED),
    ),
    Phase(
        id="publish",
        title="Publish",
        blurb="Check every claim against the source, then it is yours to approve.",
        states=(S.FINAL_VALIDATING, S.HUMAN_APPROVAL_REQUIRED),
    ),
)

#: What is happening, in one line, for every state the machine has.
#:
#: Written in the second person where a person is the one holding it up, because
#: the difference between "we are working" and "you are the hold-up" is the most
#: useful thing an interface can tell somebody who has left this open in a tab.
STATE_HEADLINES: Mapping[S, str] = {
    S.SOURCE_INGESTED: "Source material is in. Build the source model when you are ready.",
    S.SOURCE_MODEL_EXTRACTING: "Reading the source and working out what it does not say.",
    S.SOURCE_QUESTIONS_REQUIRED: "Your turn: answer what the source could not.",
    S.SOURCE_MODEL_READY: "The source model holds. Ready to decide what to write.",
    S.ARCHITECTURE_PROPOSING: "Working out what this material supports.",
    S.ARCHITECTURE_REVIEW_REQUIRED: "Your turn: approve the shape of the work, or send it back.",
    S.ARCHITECTURE_APPROVED: "Shape approved. Ready to brief the first article.",
    S.BRIEF_GENERATING: "Writing the brief this article will be held to.",
    S.BRIEF_REVIEW_REQUIRED: "Your turn: approve the brief, or send it back.",
    S.DRAFT_GENERATING: "Drafting against the brief and the source model.",
    S.SUBSTANTIVE_REVIEWING: "Reviewing the argument, not the prose.",
    S.REVISION_PLAN_REQUIRED: "Your turn: approve the plan for what the rewrite will change.",
    S.SUBSTANTIVE_REWRITING: "Rewriting under the approved plan.",
    S.VOICE_ALIGNING: "Aligning the prose to your voice profile.",
    S.SCORING: "Grading the article against the rubric.",
    S.REVISION_REQUIRED: "The score came back short. Another round is being routed.",
    S.PASSED: "It passed the rubric. Ready for final validation.",
    S.STALLED: "Your turn: rounds are not improving it, so the next move is yours.",
    S.FINAL_VALIDATING: "Checking every claim against the source it came from.",
    S.HUMAN_APPROVAL_REQUIRED: "Your turn: the last word on publishing is yours.",
    S.COMPLETED: "Published. Every version and decision is kept.",
    S.FAILED: "The run stopped on an error it could not recover from.",
    S.CANCELLED: "You stopped this run. Its record is kept.",
}


def phase_of(state: S) -> Phase | None:
    """The phase ``state`` belongs to, or ``None`` for an ending."""
    return next((phase for phase in PHASES if state in phase.states), None)


def journey_of(state: S) -> tuple[Step, ...]:
    """The whole strip, seen from ``state``.

    A run that stopped short claims nothing: its state says where it *was*
    stopped, not how far it had got, and a strip that guessed would be inventing
    progress. ``COMPLETED`` is the opposite case and the only one where every
    phase is behind you.
    """
    if state is S.COMPLETED:
        return tuple(Step(p.id, p.title, p.blurb, "done") for p in PHASES)

    current = phase_of(state)
    if current is None:
        return tuple(Step(p.id, p.title, p.blurb, "upcoming") for p in PHASES)

    reached = PHASES.index(current)
    return tuple(
        Step(
            phase.id,
            phase.title,
            phase.blurb,
            "done" if index < reached else "current" if index == reached else "upcoming",
        )
        for index, phase in enumerate(PHASES)
    )


def waiting_on(state: S) -> str:
    """Who the run is waiting for: ``"you"``, ``"pipeline"`` or ``"nobody"``.

    The human-pause answer, asked of the transition table rather than kept as a
    second list here. A state parks for a person exactly when every edge out of
    it is a person's to take, which is a property of the edges — and one that
    changes the day somebody adds an automatic escape from a pause.
    """
    if state in TERMINAL_STATES:
        return "nobody"
    return "you" if is_human_pause(state) else "pipeline"


__all__ = [
    "ENDINGS",
    "PHASES",
    "STATE_HEADLINES",
    "Phase",
    "Step",
    "journey_of",
    "phase_of",
    "waiting_on",
]
