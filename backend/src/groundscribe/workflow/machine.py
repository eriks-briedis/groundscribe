"""The pure state machine: apply an action, get a new state (phase 05).

No database, no recorder, no clock. Everything phase 05 must *prove* — that
illegal transitions are rejected, that the engine cannot step past a human
pause, that nothing reaches ``COMPLETED`` without final validation — is a
property of this object alone, and proving it here means the property tests can
run thousands of action sequences without touching a session.

:class:`~groundscribe.workflow.engine.WorkflowEngine` wraps this with
provenance. The division of labour: the machine decides *whether* a move is
legal, the engine records *that it happened* and enforces the guards that need
stored artefacts to check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from groundscribe.provenance.enums import ActorType
from groundscribe.workflow.errors import (
    AmbiguousTransition,
    HumanActionRequired,
    IllegalTransition,
)
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import (
    TERMINAL_STATES,
    Transition,
    available_actions,
    is_human_pause,
    targets_for,
    transition_for,
)


@dataclass(frozen=True)
class TransitionOutcome:
    """One applied move: the edge taken and the states either side of it.

    Carries the whole :class:`Transition` rather than just the target so the
    engine can record the edge's actor and rationale without looking the row up
    again — a second lookup is a second chance to disagree with the first.
    """

    transition: Transition
    previous_state: WorkflowState
    state: WorkflowState

    @property
    def action(self) -> WorkflowAction:
        return self.transition.action


@dataclass
class WorkflowMachine:
    """A run's position in the workflow, and the rules for changing it.

    Mutable by design: a run *is* a moving position, and threading an immutable
    machine through callers would push the "which one is current?" question onto
    every one of them. The transition *table* stays immutable, which is the part
    that must not vary between runs.
    """

    state: WorkflowState = WorkflowState.SOURCE_INGESTED
    history: list[TransitionOutcome] = field(default_factory=list)

    @property
    def is_paused(self) -> bool:
        """Whether the run is parked waiting for a person."""
        return is_human_pause(self.state)

    def available_actions(self) -> tuple[WorkflowAction, ...]:
        """The actions offered in the current state."""
        return available_actions(self.state)

    def targets(self, action: WorkflowAction) -> tuple[WorkflowState, ...]:
        """Where ``action`` may lead from the current state."""
        return targets_for(self.state, action)

    def apply(
        self,
        action: WorkflowAction,
        *,
        target: WorkflowState | None = None,
        actor: ActorType = ActorType.POLICY,
    ) -> TransitionOutcome:
        """Take ``action``, moving the run, or refuse and leave it where it is.

        ``target`` may be omitted when the action leads exactly one place; for a
        multi-destination action such as ``ROUTE_REVISION`` it is required,
        because a machine that guessed one of seven destinations would make the
        routing policy's choice unenforceable.

        The actor guard runs one way: a user-only edge demands a ``USER`` actor,
        while a policy edge accepts anyone. That asymmetry is the human pause —
        the engine, acting as a policy, can never step past a review state, but
        a person driving the run by hand is not locked out of ordinary progress.
        """
        transition = self._resolve(action, target)
        if transition.actor is ActorType.USER and actor is not ActorType.USER:
            raise HumanActionRequired(
                f"{self.state.value} → {transition.target.value} via {action.value} "
                f"requires a user; {actor.value} may not take it"
            )

        outcome = TransitionOutcome(
            transition=transition, previous_state=self.state, state=transition.target
        )
        self.state = transition.target
        self.history.append(outcome)
        return outcome

    def _resolve(self, action: WorkflowAction, target: WorkflowState | None) -> Transition:
        """Find the one edge this call means, or explain why there isn't one."""
        if self.state in TERMINAL_STATES:
            raise IllegalTransition(
                f"{self.state.value} is terminal; {action.value} cannot be applied"
            )

        candidates = targets_for(self.state, action)
        if not candidates:
            offered = ", ".join(sorted(a.value for a in self.available_actions()))
            raise IllegalTransition(
                f"{action.value} is not available in {self.state.value} (offered: {offered})"
            )

        if target is None:
            if len(candidates) > 1:
                choices = ", ".join(sorted(state.value for state in candidates))
                raise AmbiguousTransition(
                    f"{action.value} from {self.state.value} leads to several states "
                    f"({choices}); name the target"
                )
            target = candidates[0]

        transition = transition_for(self.state, action, target)
        if transition is None:
            choices = ", ".join(sorted(state.value for state in candidates))
            raise IllegalTransition(
                f"{action.value} from {self.state.value} cannot lead to {target.value} "
                f"(legal targets: {choices})"
            )
        return transition


__all__ = ["TransitionOutcome", "WorkflowMachine"]
