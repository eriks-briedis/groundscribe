"""The versioned workflow policy: routing, limits and stagnation (phase 05).

plan/05 → *Routing policy (versioned)*, *Rewrite limits (versioned defaults)*,
*Stagnation detection*; and its Risks section: "encoding routing thresholds
inline — keep them in the versioned policy object, shared with phase 08".

Three things live here because they are one decision seen from three angles:
where a failure goes, how many times it may go there, and when going there has
stopped helping. Splitting them across three files would let a deployment ship
a routing change without the limit change that makes it safe.

The policy is loaded from a file for the reason phase 04's model routing is:
these are editorial judgements an author should be able to read, diff and tune
without a deploy. It is *versioned* because every routing decision records the
version that made it — phase 03 refuses to store a policy decision that cannot
name one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from groundscribe.paths import config_root
from groundscribe.workflow.errors import WorkflowError
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import targets_for

#: Filename of the shipped workflow policy under the config root.
WORKFLOW_POLICY_FILENAME = "workflow-policy.yaml"


class WorkflowPolicyError(WorkflowError):
    """The policy is missing, malformed, or was asked for a route it forbids."""


class FailureCategory(StrEnum):
    """What kind of problem a review or score found.

    The categories are the spec's routing rules read backwards: each one exists
    because it is corrected in a *different* stage. A category that routed
    somewhere an existing one already routes would not be a category, it would
    be a synonym.
    """

    FACTUAL_GAP = "factual_gap"
    ARCHITECTURE_ISSUE = "architecture_issue"
    SUBSTANTIVE_ISSUE = "substantive_issue"
    STYLE_ISSUE = "style_issue"
    MINOR_LOCAL = "minor_local"


class LimitKind(StrEnum):
    """Which bounded loop a route spends a round of.

    Three counters, matching the spec's three limits. Not one counter per
    category: "how many substantive rewrites has this article had" is the
    question the limit answers, and a targeted patch is still a rewrite.
    """

    SUBSTANTIVE = "substantive"
    STYLE = "style"
    ARCHITECTURE = "architecture"


#: The states a failure may legally be routed to, per the transition table.
def _routable_states() -> tuple[WorkflowState, ...]:
    return targets_for(WorkflowState.REVISION_REQUIRED, WorkflowAction.ROUTE_REVISION)


class RoutingRule(BaseModel):
    """Where one failure class goes, and what it costs.

    ``alternatives`` is how the spec's "source extraction / questions" and
    "architecture / brief" pairs are expressed: one rule with a default end and
    the other legal ends beside it. Modelling them as separate categories would
    force the caller to know which correcting stage applies — which is exactly
    the judgement the policy exists to hold.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: WorkflowState
    alternatives: tuple[WorkflowState, ...] = ()
    limit: LimitKind | None = None
    rationale: str = ""

    @property
    def targets(self) -> tuple[WorkflowState, ...]:
        """Every state this rule may route to, default first."""
        return (self.target, *self.alternatives)

    def permits(self, state: WorkflowState) -> bool:
        return state in self.targets


class RewriteLimits(BaseModel):
    """plan/05: max 3 substantive rewrites, 2 style-only, 1 architecture reopening.

    Defaults are the spec's numbers, so a config that omits them is still
    correct rather than unbounded — the failure mode of a missing limit is an
    infinite loop that burns a model budget.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    substantive: int = Field(default=3, ge=0)
    style: int = Field(default=2, ge=0)
    architecture: int = Field(default=1, ge=0)

    def maximum(self, kind: LimitKind) -> int:
        """How many automatic rounds ``kind`` allows before a person must decide."""
        return {
            LimitKind.SUBSTANTIVE: self.substantive,
            LimitKind.STYLE: self.style,
            LimitKind.ARCHITECTURE: self.architecture,
        }[kind]


class StagnationThresholds(BaseModel):
    """When the revision loop has stopped being worth another round.

    plan/05 lists six conditions; these are the numbers they compare against.
    Held in the policy rather than in the detector so that tuning them is a
    config change with a version bump, and every stalled run can say which
    thresholds stalled it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Points of overall improvement below which a round counts as no progress.
    min_improvement: float = Field(default=2.0, ge=0)
    #: How many consecutive no-progress rounds are needed to call it stagnant.
    improvement_rounds: int = Field(default=2, ge=1)
    #: How many rewrites a blocking issue may survive before it is entrenched.
    blocking_issue_rounds: int = Field(default=2, ge=1)
    #: Points by which one dimension may improve while another falls.
    dimension_divergence: float = Field(default=5.0, ge=0)
    #: Manual edit distance (0-1) above which a voice pass is not working.
    max_edit_distance: float = Field(default=0.3, ge=0, le=1)
    #: Voice passes after which the edit-distance check starts applying.
    voice_pass_rounds: int = Field(default=2, ge=1)


class SourceQuestionLimits(BaseModel):
    """How much the pipeline may ask the author before it gets on with it.

    The gap loop is the one cycle in this workflow that had no bound. Answers do
    not patch the source model — they re-enter extraction, which regenerates the
    gap report, which finds fresh blocking gaps and parks the run again. A source
    of any size always has *something* absent, so the cycle terminated only if the
    model happened to run out of things to ask.

    Two separate ceilings, because two separate things were unbounded:

    ``max_rounds`` bounds how many times a person is sent back to the queue.
    ``max_surfaced_per_round`` bounds how many questions arrive at once, which
    the gap module's own docstring already identified as the thing that stops
    answers coming: "an author faced with fifteen questions answers none". It had
    a suppression policy for high-value and optional gaps and none for blocking
    ones, which is how a round of fifteen happens.

    Unasked gaps are not lost. They are stored like every other, with
    ``surfaced`` false, and stay visible as unresolved — a run proceeds *knowing*
    what it does not know, which is the honest version of proceeding. Same
    principle as the rewrite limits above: the numbers bound the machine, not the
    author, who can always answer more and re-run extraction deliberately.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Rounds of questions before extraction completes regardless. Zero means
    #: never ask, which is a legitimate configuration for a source nobody can
    #: add to — an archive, someone else's document — and not the default.
    max_rounds: int = Field(default=1, ge=0)
    #: How many questions one round may put to the author.
    max_surfaced_per_round: int = Field(default=5, ge=1)


@dataclass(frozen=True)
class RoutingOutcome:
    """One routing resolution, with everything its decision record needs.

    Carries the policy version and the limit consumed alongside the target, so
    the engine writes the decision without a second lookup — the same reason
    phase 04's :class:`~groundscribe.llm.routing.ResolvedRoute` does.
    """

    category: FailureCategory
    target: WorkflowState
    policy_version: str
    limit: LimitKind | None = None
    rationale: str = ""
    preferred: WorkflowState | None = None


class WorkflowPolicy(BaseModel):
    """The versioned rules the engine consults when a state offers a choice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    description: str = ""
    routing: dict[FailureCategory, RoutingRule]
    limits: RewriteLimits = RewriteLimits()
    stagnation: StagnationThresholds = StagnationThresholds()
    source_questions: SourceQuestionLimits = SourceQuestionLimits()

    @model_validator(mode="after")
    def _routes_every_failure_to_a_reachable_stage(self) -> Self:
        """Refuse a policy that is silent or impossible, at load rather than mid-run.

        Two checks. Every failure class must route somewhere: a category with no
        rule would surface as a ``KeyError`` in the middle of a scored article.
        And every named state must be one the transition table actually permits
        from ``REVISION_REQUIRED``: the policy chooses among the table's edges,
        it cannot invent one, and a typo caught here is a startup error instead
        of an illegal-transition failure three stages into a run.
        """
        missing = sorted(
            category.value for category in FailureCategory if category not in self.routing
        )
        if missing:
            raise ValueError(f"routing is missing rules for: {', '.join(missing)}")

        routable = _routable_states()
        for category, rule in self.routing.items():
            for state in rule.targets:
                if state not in routable:
                    raise ValueError(
                        f"routing rule {category.value!r} targets {state.value}, which "
                        f"{WorkflowState.REVISION_REQUIRED.value} cannot reach via "
                        f"{WorkflowAction.ROUTE_REVISION.value}"
                    )
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> WorkflowPolicy:
        """Load a workflow policy from a YAML file."""
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise WorkflowPolicyError(f"cannot read workflow policy {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise WorkflowPolicyError(f"invalid YAML in workflow policy {path}: {exc}") from exc
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise WorkflowPolicyError(f"invalid workflow policy {path}: {exc}") from exc

    def rule(self, category: FailureCategory) -> RoutingRule:
        """The rule for ``category``; validated to exist at load time."""
        rule = self.routing.get(category)
        if rule is None:  # pragma: no cover - the validator forbids this
            raise WorkflowPolicyError(f"no routing rule for {category.value}")
        return rule

    def resolve(
        self, category: FailureCategory, *, prefer: WorkflowState | None = None
    ) -> RoutingOutcome:
        """Route one failure to the stage that can correct it.

        ``prefer`` picks a declared alternative — "ask the author" rather than
        "re-extract" — and is refused when the rule does not permit it. That
        refusal is what makes plan/05's invariants enforceable: a factual
        failure cannot be answered with a voice pass, and a style-only failure
        cannot reopen the source model, even when a caller asks for it.
        """
        rule = self.rule(category)
        if prefer is not None and not rule.permits(prefer):
            allowed = ", ".join(state.value for state in rule.targets)
            raise WorkflowPolicyError(
                f"{category.value} cannot be routed to {prefer.value} (allowed: {allowed})"
            )
        return RoutingOutcome(
            category=category,
            target=prefer or rule.target,
            policy_version=self.version,
            limit=rule.limit,
            rationale=rule.rationale,
            preferred=prefer,
        )


def default_workflow_policy() -> WorkflowPolicy:
    """The shipped workflow policy from the config root."""
    return WorkflowPolicy.from_yaml(config_root() / WORKFLOW_POLICY_FILENAME)


__all__ = [
    "WORKFLOW_POLICY_FILENAME",
    "FailureCategory",
    "LimitKind",
    "RewriteLimits",
    "RoutingOutcome",
    "RoutingRule",
    "SourceQuestionLimits",
    "StagnationThresholds",
    "WorkflowPolicy",
    "WorkflowPolicyError",
    "default_workflow_policy",
]
