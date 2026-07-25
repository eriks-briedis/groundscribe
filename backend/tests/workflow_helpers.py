"""Shared builders for the phase-05 workflow tests.

Not a conftest, for the reason ``provenance_helpers`` is not one: an import
error while the subsystem is being built should fail the modules that use it,
not collection for the whole suite.

The policy here is a throwaway that mirrors the shipped one. Tests that assert
routing behaviour must not change meaning when an editorial judgement in
``config/workflow-policy.yaml`` is tuned; two tests in ``test_workflow_routing``
hold the shipped file to the contract, and that is the only place it belongs.

Nothing here is named ``test_*``: pytest collects such names out of a test
module's namespace even when they arrived by import.
"""

from __future__ import annotations

from groundscribe.provenance.enums import ActorType
from groundscribe.workflow.machine import WorkflowMachine
from groundscribe.workflow.policy import (
    FailureCategory,
    LimitKind,
    RewriteLimits,
    RoutingRule,
    StagnationThresholds,
    WorkflowPolicy,
)
from groundscribe.workflow.states import WorkflowAction, WorkflowState

A = WorkflowAction
S = WorkflowState


def sample_policy(
    *,
    version: str = "test-1",
    limits: RewriteLimits | None = None,
    stagnation: StagnationThresholds | None = None,
) -> WorkflowPolicy:
    """A policy with the spec's routes, so tests can vary one thing at a time."""
    return WorkflowPolicy(
        version=version,
        routing={
            FailureCategory.FACTUAL_GAP: RoutingRule(
                target=S.SOURCE_MODEL_EXTRACTING,
                alternatives=(S.SOURCE_QUESTIONS_REQUIRED,),
            ),
            FailureCategory.ARCHITECTURE_ISSUE: RoutingRule(
                target=S.ARCHITECTURE_PROPOSING,
                alternatives=(S.BRIEF_GENERATING,),
                limit=LimitKind.ARCHITECTURE,
            ),
            FailureCategory.SUBSTANTIVE_ISSUE: RoutingRule(
                target=S.REVISION_PLAN_REQUIRED,
                alternatives=(S.SUBSTANTIVE_REWRITING,),
                limit=LimitKind.SUBSTANTIVE,
            ),
            FailureCategory.STYLE_ISSUE: RoutingRule(
                target=S.VOICE_ALIGNING, limit=LimitKind.STYLE
            ),
            FailureCategory.MINOR_LOCAL: RoutingRule(
                target=S.SUBSTANTIVE_REWRITING, limit=LimitKind.SUBSTANTIVE
            ),
        },
        limits=limits or RewriteLimits(),
        stagnation=stagnation or StagnationThresholds(),
    )


def machine_at(state: WorkflowState, *, policy: WorkflowPolicy | None = None) -> WorkflowMachine:
    """A machine parked at ``state`` running the throwaway policy."""
    return WorkflowMachine(state=state, policy=policy or sample_policy())


#: The steps from wherever a route lands back to ``SCORING``. Walking the real
#: edges rather than assigning ``machine.state`` keeps the tests honest about
#: whether the table actually composes into a loop.
_BACK_TO_SCORING: dict[WorkflowState, tuple[tuple[WorkflowAction, ActorType], ...]] = {
    S.SOURCE_QUESTIONS_REQUIRED: ((A.ANSWER_QUESTIONS, ActorType.USER),),
    S.SOURCE_MODEL_EXTRACTING: (
        (A.COMPLETE_EXTRACTION, ActorType.POLICY),
        (A.PROPOSE_ARCHITECTURE, ActorType.POLICY),
    ),
    S.ARCHITECTURE_PROPOSING: (
        (A.SUBMIT_ARCHITECTURE, ActorType.POLICY),
        (A.APPROVE_ARCHITECTURE, ActorType.USER),
        (A.GENERATE_BRIEF, ActorType.POLICY),
    ),
    S.BRIEF_GENERATING: (
        (A.SUBMIT_BRIEF, ActorType.POLICY),
        (A.APPROVE_BRIEF, ActorType.USER),
        (A.SUBMIT_DRAFT, ActorType.POLICY),
        (A.ACCEPT_REVIEW, ActorType.POLICY),
        (A.SUBMIT_VOICE_PASS, ActorType.POLICY),
    ),
    S.REVISION_PLAN_REQUIRED: ((A.APPROVE_REVISION_PLAN, ActorType.USER),),
    S.SUBSTANTIVE_REWRITING: (
        (A.SUBMIT_REWRITE, ActorType.POLICY),
        (A.ACCEPT_REVIEW, ActorType.POLICY),
        (A.SUBMIT_VOICE_PASS, ActorType.POLICY),
    ),
    S.VOICE_ALIGNING: ((A.SUBMIT_VOICE_PASS, ActorType.POLICY),),
}


def advance_to_scoring(machine: WorkflowMachine) -> None:
    """Walk the loop from the current state until the article is scored again."""
    while machine.state is not S.SCORING:
        steps = _BACK_TO_SCORING.get(machine.state)
        if steps is None:  # pragma: no cover - a mapping gap is a test bug
            raise AssertionError(f"no path back to scoring from {machine.state.value}")
        for action, actor in steps:
            machine.apply(action, actor=actor)


def fail_again(machine: WorkflowMachine) -> None:
    """Send the article round the loop once more and fail it again.

    The only way to reach the next routing decision, so limit tests exercise the
    real cycle instead of re-entering ``REVISION_REQUIRED`` by assignment.
    """
    advance_to_scoring(machine)
    machine.apply(A.SCORE_FAILED)
