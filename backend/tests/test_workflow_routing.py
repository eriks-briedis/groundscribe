"""The versioned routing policy: failure class → correcting stage (phase 05).

Spec (plan/05):

- a **versioned routing policy** — factual gap → source extraction / questions;
  architecture issue → architecture / brief; substantive issue → revision
  planning / rewrite; style issue → voice alignment; minor local → targeted
  patch; pass → final validation;
- invariants: *a failed factual-fidelity score cannot route only to style
  editing*, and *a style-only failure must not trigger source extraction*;
- plan/05 Risks: thresholds and routes stay in the versioned policy object,
  never inline — phase 08 shares this object.

The rules are exercised against a throwaway config so the tests state the
contract rather than today's routing choices; the last tests hold the shipped
``config/workflow-policy.yaml`` to that contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundscribe.workflow.policy import (
    FailureCategory,
    LimitKind,
    WorkflowPolicy,
    WorkflowPolicyError,
    default_workflow_policy,
)
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import targets_for

S = WorkflowState
C = FailureCategory

CONFIG = """
version: "test-1"
description: Routing used by the routing tests.
routing:
  factual_gap:
    target: source_model_extracting
    alternatives: [source_questions_required]
    rationale: Re-extract from source; ask the author only when the source is silent.
  architecture_issue:
    target: architecture_proposing
    alternatives: [brief_generating]
    limit: architecture
  substantive_issue:
    target: revision_plan_required
    alternatives: [substantive_rewriting]
    limit: substantive
  style_issue:
    target: voice_aligning
    limit: style
  minor_local:
    target: substantive_rewriting
    limit: substantive
limits:
  substantive: 3
  style: 2
  architecture: 1
stagnation:
  min_improvement: 2.0
  improvement_rounds: 2
  blocking_issue_rounds: 2
  dimension_divergence: 5.0
  max_edit_distance: 0.3
  voice_pass_rounds: 2
"""


@pytest.fixture
def policy(tmp_path: Path) -> WorkflowPolicy:
    path = tmp_path / "workflow-policy.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return WorkflowPolicy.from_yaml(path)


def test_the_policy_carries_its_version(policy: WorkflowPolicy) -> None:
    """A routing decision that cannot name its policy version is unreviewable."""
    assert policy.version == "test-1"


@pytest.mark.parametrize(
    ("category", "target"),
    [
        (C.FACTUAL_GAP, S.SOURCE_MODEL_EXTRACTING),
        (C.ARCHITECTURE_ISSUE, S.ARCHITECTURE_PROPOSING),
        (C.SUBSTANTIVE_ISSUE, S.REVISION_PLAN_REQUIRED),
        (C.STYLE_ISSUE, S.VOICE_ALIGNING),
        (C.MINOR_LOCAL, S.SUBSTANTIVE_REWRITING),
    ],
)
def test_each_failure_class_routes_to_its_correcting_stage(
    policy: WorkflowPolicy, category: FailureCategory, target: WorkflowState
) -> None:
    outcome = policy.resolve(category)
    assert outcome.target is target
    assert outcome.category is category
    assert outcome.policy_version == "test-1"


@pytest.mark.parametrize(
    ("category", "alternative"),
    [
        (C.FACTUAL_GAP, S.SOURCE_QUESTIONS_REQUIRED),
        (C.ARCHITECTURE_ISSUE, S.BRIEF_GENERATING),
        (C.SUBSTANTIVE_ISSUE, S.SUBSTANTIVE_REWRITING),
    ],
)
def test_a_category_may_be_routed_to_a_declared_alternative(
    policy: WorkflowPolicy, category: FailureCategory, alternative: WorkflowState
) -> None:
    """ "source extraction *or* questions" is one rule with two legal ends."""
    outcome = policy.resolve(category, prefer=alternative)
    assert outcome.target is alternative
    assert outcome.preferred is alternative


def test_a_failed_factual_score_cannot_route_to_style_editing(policy: WorkflowPolicy) -> None:
    """plan/05 invariant: a factual failure is never answered with a voice pass."""
    assert S.VOICE_ALIGNING not in policy.rule(C.FACTUAL_GAP).targets
    with pytest.raises(WorkflowPolicyError):
        policy.resolve(C.FACTUAL_GAP, prefer=S.VOICE_ALIGNING)


def test_a_style_only_failure_cannot_trigger_source_extraction(policy: WorkflowPolicy) -> None:
    """plan/05 invariant: restyling prose must not reopen the source model."""
    rule = policy.rule(C.STYLE_ISSUE)
    assert S.SOURCE_MODEL_EXTRACTING not in rule.targets
    assert S.SOURCE_QUESTIONS_REQUIRED not in rule.targets
    with pytest.raises(WorkflowPolicyError):
        policy.resolve(C.STYLE_ISSUE, prefer=S.SOURCE_MODEL_EXTRACTING)


def test_a_preferred_target_outside_the_rule_is_refused(policy: WorkflowPolicy) -> None:
    with pytest.raises(WorkflowPolicyError):
        policy.resolve(C.MINOR_LOCAL, prefer=S.COMPLETED)


def test_the_outcome_names_the_limit_the_route_consumes(policy: WorkflowPolicy) -> None:
    """Which counter a route spends is policy, not something the engine decides."""
    assert policy.resolve(C.SUBSTANTIVE_ISSUE).limit is LimitKind.SUBSTANTIVE
    assert policy.resolve(C.STYLE_ISSUE).limit is LimitKind.STYLE
    assert policy.resolve(C.ARCHITECTURE_ISSUE).limit is LimitKind.ARCHITECTURE
    assert policy.resolve(C.MINOR_LOCAL).limit is LimitKind.SUBSTANTIVE
    assert policy.resolve(C.FACTUAL_GAP).limit is None


def test_the_rewrite_limits_are_the_spec_defaults(policy: WorkflowPolicy) -> None:
    assert policy.limits.maximum(LimitKind.SUBSTANTIVE) == 3
    assert policy.limits.maximum(LimitKind.STYLE) == 2
    assert policy.limits.maximum(LimitKind.ARCHITECTURE) == 1


def test_the_stagnation_thresholds_load(policy: WorkflowPolicy) -> None:
    assert policy.stagnation.min_improvement == pytest.approx(2.0)
    assert policy.stagnation.improvement_rounds == 2


def test_a_config_missing_a_failure_class_is_refused(tmp_path: Path) -> None:
    """Every failure class must route somewhere; silence is not a route."""
    path = tmp_path / "partial.yaml"
    path.write_text(
        CONFIG.replace("  style_issue:\n    target: voice_aligning\n    limit: style\n", ""),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="style_issue"):
        WorkflowPolicy.from_yaml(path)


def test_a_route_the_transition_table_forbids_is_refused(tmp_path: Path) -> None:
    """The policy chooses among the table's edges; it cannot invent one.

    Without this check a typo in configuration would surface much later, as an
    illegal-transition error inside a run rather than a bad config at load.
    """
    path = tmp_path / "impossible.yaml"
    path.write_text(CONFIG.replace("target: voice_aligning", "target: completed"), encoding="utf-8")
    with pytest.raises(WorkflowPolicyError, match="completed"):
        WorkflowPolicy.from_yaml(path)


def test_a_missing_config_file_is_reported_as_a_policy_error(tmp_path: Path) -> None:
    with pytest.raises(WorkflowPolicyError):
        WorkflowPolicy.from_yaml(tmp_path / "absent.yaml")


def test_malformed_yaml_is_reported_as_a_policy_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("version: [unclosed\n", encoding="utf-8")
    with pytest.raises(WorkflowPolicyError):
        WorkflowPolicy.from_yaml(path)


def test_a_passing_score_needs_no_routing_rule() -> None:
    """plan/05's "pass → final validation" is the table's path, not a rule.

    Routing exists to send a *failure* to the stage that can correct it. A pass
    has nothing to correct, so it is an ordinary edge —
    ``SCORING → PASSED → FINAL_VALIDATING`` — and duplicating it as a policy
    entry would give two places to change one behaviour.
    """
    assert targets_for(S.SCORING, WorkflowAction.SCORE_PASSED) == (S.PASSED,)
    assert targets_for(S.PASSED, WorkflowAction.VALIDATE_FINAL) == (S.FINAL_VALIDATING,)


def test_the_shipped_policy_loads_and_covers_every_failure_class() -> None:
    """The config the application actually runs on must satisfy the contract."""
    shipped = default_workflow_policy()
    assert shipped.version
    for category in FailureCategory:
        assert shipped.rule(category).targets


def test_the_shipped_policy_carries_the_spec_limits() -> None:
    """plan/05: max 3 substantive rewrites, 2 style-only, 1 architecture reopening."""
    limits = default_workflow_policy().limits
    assert (limits.substantive, limits.style, limits.architecture) == (3, 2, 1)
