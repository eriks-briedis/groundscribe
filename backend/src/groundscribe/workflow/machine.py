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
from groundscribe.workflow.policy import (
    FailureCategory,
    LimitKind,
    RewriteLimits,
    RoutingOutcome,
    WorkflowPolicy,
    default_workflow_policy,
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

#: User escalations that spend a bounded round, and which counter each spends.
#:
#: Declared here rather than as a column on the transition row because *which*
#: limit a move spends is the policy's judgement, and a table holding a second
#: opinion on it would eventually disagree with the policy. These two are the
#: exception the policy cannot cover: they are taken by a person out of a pause,
#: with no failure category to resolve. Being user edges, each is its own
#: approval — nobody else needs to authorise what a person has just chosen.
_ESCALATION_LIMITS: dict[WorkflowAction, LimitKind] = {
    WorkflowAction.AUTHORISE_REWRITE: LimitKind.SUBSTANTIVE,
    WorkflowAction.REOPEN_ARCHITECTURE: LimitKind.ARCHITECTURE,
}


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


@dataclass(frozen=True)
class RewriteApproval:
    """A person authorising a round the limits would otherwise refuse.

    ``approved_by`` is mandatory. The approval is written as a decision record,
    and phase 03 refuses to store a decision nobody is accountable for — so an
    anonymous approval is rejected here rather than failing later at the write,
    the same rule phase 04's :class:`~groundscribe.llm.routing.RouteOverride`
    applies to model overrides.
    """

    approved_by: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.approved_by:
            raise ValueError("approved_by is required: an anonymous approval is unreviewable")


@dataclass
class RewriteLedger:
    """How many bounded rounds a run has spent, and how many were granted.

    Two counters per limit rather than one net figure. "Three rewrites, none
    approved" and "three rewrites, two of them authorised by the author" are
    different runs, and a single number that netted them off would erase the
    only evidence that a person was ever consulted.
    """

    limits: RewriteLimits
    rounds: dict[LimitKind, int] = field(default_factory=dict)
    grants: dict[LimitKind, int] = field(default_factory=dict)

    def spent(self, kind: LimitKind) -> int:
        """Rounds of ``kind`` this run has used."""
        return self.rounds.get(kind, 0)

    def approved(self, kind: LimitKind) -> int:
        """Extra rounds of ``kind`` a person has explicitly authorised."""
        return self.grants.get(kind, 0)

    def allowance(self, kind: LimitKind) -> int:
        """The policy's limit plus every round a person has since granted."""
        return self.limits.maximum(kind) + self.approved(kind)

    def may_spend(self, kind: LimitKind) -> bool:
        """Whether another round of ``kind`` may run without asking anyone."""
        return self.spent(kind) < self.allowance(kind)

    def grant(self, kind: LimitKind) -> None:
        """Record that a person authorised one more round of ``kind``."""
        self.grants[kind] = self.approved(kind) + 1

    def spend(self, kind: LimitKind) -> None:
        """Charge one round of ``kind`` to the run."""
        self.rounds[kind] = self.spent(kind) + 1


@dataclass(frozen=True)
class RouteResult:
    """What routing a failure did: where it went, or why it could not.

    ``escalated`` is not an error. A run that has spent its rounds parks at
    ``STALLED`` for a person, which is a legitimate outcome of routing rather
    than a failure of it — the alternative, raising, would make the caller
    responsible for parking the run, which is the engine's job.
    """

    outcome: RoutingOutcome
    transition: TransitionOutcome
    escalated: bool = False
    approval: RewriteApproval | None = None
    reason: str = ""


@dataclass
class WorkflowMachine:
    """A run's position in the workflow, and the rules for changing it.

    Mutable by design: a run *is* a moving position, and threading an immutable
    machine through callers would push the "which one is current?" question onto
    every one of them. The transition *table* stays immutable, which is the part
    that must not vary between runs.
    """

    state: WorkflowState = WorkflowState.SOURCE_INGESTED
    policy: WorkflowPolicy = field(default_factory=default_workflow_policy)
    history: list[TransitionOutcome] = field(default_factory=list)
    ledger: RewriteLedger = field(init=False)

    def __post_init__(self) -> None:
        # Derived rather than passed: a ledger whose limits disagreed with the
        # policy the machine routes under would enforce a rule nobody declared.
        self.ledger = RewriteLedger(self.policy.limits)

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

        escalated = _ESCALATION_LIMITS.get(action)
        if escalated is not None and transition.actor is ActorType.USER:
            self.ledger.grant(escalated)
            self.ledger.spend(escalated)
        return outcome

    def route(
        self,
        category: FailureCategory,
        *,
        prefer: WorkflowState | None = None,
        approval: RewriteApproval | None = None,
    ) -> RouteResult:
        """Send a failing article to the stage that can correct it.

        Three things happen in order, and the order is the point: the policy
        decides *where* (so the spec's routing invariants hold), the table
        confirms the edge exists (so no policy typo moves the run), and only
        then does the limit decide whether it may run now.

        Over the limit and unapproved, the run parks at ``STALLED`` instead —
        a decision, not an exception. Raising would leave the caller holding a
        run in ``REVISION_REQUIRED`` with nothing to do next, and the caller is
        not the place where "what happens when the loop runs out" is decided.
        """
        outcome = self.policy.resolve(category, prefer=prefer)
        # Resolve before touching the ledger: an impossible route must not
        # charge a round on its way to being rejected.
        self._resolve(WorkflowAction.ROUTE_REVISION, outcome.target)

        limit = outcome.limit
        if limit is not None and not self.ledger.may_spend(limit):
            if approval is None:
                reason = (
                    f"{limit.value} rewrites are capped at {self.ledger.allowance(limit)} "
                    f"and {self.ledger.spent(limit)} have been used; a person must decide"
                )
                return RouteResult(
                    outcome=outcome,
                    transition=self.apply(WorkflowAction.STALL, target=WorkflowState.STALLED),
                    escalated=True,
                    reason=reason,
                )
            self.ledger.grant(limit)
        else:
            # An approval offered when none was needed is left unspent: it
            # would otherwise silently raise the ceiling for a later round
            # nobody has looked at yet.
            approval = None

        transition = self.apply(WorkflowAction.ROUTE_REVISION, target=outcome.target)
        if limit is not None:
            self.ledger.spend(limit)
        return RouteResult(outcome=outcome, transition=transition, approval=approval)

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


__all__ = [
    "RewriteApproval",
    "RewriteLedger",
    "RouteResult",
    "TransitionOutcome",
    "WorkflowMachine",
]
