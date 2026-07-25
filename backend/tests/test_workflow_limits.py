"""Bounded revision: rewrite limits and the approval that lifts them (phase 05).

Spec (plan/05):

- **rewrite limits (versioned defaults):** max 3 substantive rewrites, max 2
  style-only, max 1 automatic architecture reopening; *further rounds require
  explicit user approval*;
- test-first: *rewrite limits cannot be exceeded without user approval*.

Each test walks the real loop back to ``REVISION_REQUIRED`` rather than
re-entering it by assignment, so a limit that only held on a straight line
would not pass here.
"""

from __future__ import annotations

import pytest

from groundscribe.provenance.enums import ActorType
from groundscribe.workflow.errors import IllegalTransition, WorkflowError
from groundscribe.workflow.machine import RewriteApproval
from groundscribe.workflow.policy import FailureCategory, LimitKind, RewriteLimits
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from workflow_helpers import fail_again, machine_at, sample_policy

A = WorkflowAction
S = WorkflowState
C = FailureCategory


def test_routing_from_anywhere_but_revision_required_is_illegal() -> None:
    """Routing is what happens *after* a failing score, not a free action."""
    with pytest.raises(IllegalTransition):
        machine_at(S.DRAFT_GENERATING).route(C.SUBSTANTIVE_ISSUE)


def test_a_route_moves_the_machine_to_the_correcting_stage() -> None:
    machine = machine_at(S.REVISION_REQUIRED)
    result = machine.route(C.STYLE_ISSUE)
    assert machine.state is S.VOICE_ALIGNING
    assert result.outcome.target is S.VOICE_ALIGNING
    assert result.outcome.policy_version == "test-1"
    assert not result.escalated


def test_a_route_may_be_aimed_at_a_declared_alternative() -> None:
    machine = machine_at(S.REVISION_REQUIRED)
    machine.route(C.FACTUAL_GAP, prefer=S.SOURCE_QUESTIONS_REQUIRED)
    assert machine.state is S.SOURCE_QUESTIONS_REQUIRED


def test_three_substantive_rewrites_are_allowed_automatically() -> None:
    """plan/05: max 3 substantive rewrites before a person must decide."""
    machine = machine_at(S.REVISION_REQUIRED)
    for _ in range(3):
        assert not machine.route(C.SUBSTANTIVE_ISSUE).escalated
        fail_again(machine)
    assert machine.ledger.spent(LimitKind.SUBSTANTIVE) == 3


def test_the_fourth_substantive_rewrite_stalls_instead_of_running() -> None:
    """Over the limit and unapproved, the run parks for a human decision."""
    machine = machine_at(S.REVISION_REQUIRED)
    for _ in range(3):
        machine.route(C.SUBSTANTIVE_ISSUE)
        fail_again(machine)

    result = machine.route(C.SUBSTANTIVE_ISSUE)
    assert result.escalated
    assert machine.state is S.STALLED
    assert machine.is_paused
    assert result.outcome.limit is LimitKind.SUBSTANTIVE
    # The refused round is not charged: nothing was rewritten.
    assert machine.ledger.spent(LimitKind.SUBSTANTIVE) == 3


def test_an_approved_fourth_rewrite_proceeds() -> None:
    """plan/05: further rounds require *explicit user approval* — and then run."""
    machine = machine_at(S.REVISION_REQUIRED)
    for _ in range(3):
        machine.route(C.SUBSTANTIVE_ISSUE)
        fail_again(machine)

    approval = RewriteApproval(approved_by="ada", reason="the thesis finally landed")
    result = machine.route(C.SUBSTANTIVE_ISSUE, approval=approval)
    assert not result.escalated
    assert result.approval is approval
    assert machine.state is S.REVISION_PLAN_REQUIRED
    assert machine.ledger.spent(LimitKind.SUBSTANTIVE) == 4
    assert machine.ledger.approved(LimitKind.SUBSTANTIVE) == 1


def test_each_further_round_needs_its_own_approval() -> None:
    """One approval buys one round, not an unbounded licence."""
    machine = machine_at(S.REVISION_REQUIRED)
    for _ in range(3):
        machine.route(C.SUBSTANTIVE_ISSUE)
        fail_again(machine)
    machine.route(C.SUBSTANTIVE_ISSUE, approval=RewriteApproval(approved_by="ada"))
    fail_again(machine)

    assert machine.route(C.SUBSTANTIVE_ISSUE).escalated


def test_two_style_rounds_are_allowed_and_the_third_stalls() -> None:
    machine = machine_at(S.REVISION_REQUIRED)
    for _ in range(2):
        assert not machine.route(C.STYLE_ISSUE).escalated
        fail_again(machine)
    assert machine.route(C.STYLE_ISSUE).escalated


def test_one_architecture_reopening_is_allowed_and_the_second_stalls() -> None:
    """plan/05: max 1 *automatic* architecture reopening."""
    machine = machine_at(S.REVISION_REQUIRED)
    assert not machine.route(C.ARCHITECTURE_ISSUE).escalated
    fail_again(machine)
    assert machine.route(C.ARCHITECTURE_ISSUE).escalated


def test_a_targeted_patch_spends_a_substantive_round() -> None:
    """A minor local fix is a smaller rewrite, not a different kind of one."""
    machine = machine_at(S.REVISION_REQUIRED)
    machine.route(C.MINOR_LOCAL)
    assert machine.ledger.spent(LimitKind.SUBSTANTIVE) == 1


def test_factual_routing_is_never_limited() -> None:
    """Refusing to correct a fact to save a round trades away source truth."""
    machine = machine_at(S.REVISION_REQUIRED)
    for _ in range(5):
        assert not machine.route(C.FACTUAL_GAP).escalated
        fail_again(machine)


def test_the_limits_come_from_the_policy_not_from_the_engine() -> None:
    """plan/05 Risks: thresholds live in the versioned policy, never inline."""
    policy = sample_policy(limits=RewriteLimits(substantive=1, style=1, architecture=1))
    machine = machine_at(S.REVISION_REQUIRED, policy=policy)
    machine.route(C.SUBSTANTIVE_ISSUE)
    fail_again(machine)
    assert machine.route(C.SUBSTANTIVE_ISSUE).escalated


def test_an_unattributed_approval_is_refused() -> None:
    """Phase 03 will not store a decision nobody is accountable for."""
    with pytest.raises(ValueError, match="approved_by"):
        RewriteApproval(approved_by="")


def test_authorising_a_rewrite_out_of_stalled_spends_and_grants_a_round() -> None:
    """The escalation option is itself the approval it needs."""
    machine = machine_at(S.REVISION_REQUIRED)
    for _ in range(3):
        machine.route(C.SUBSTANTIVE_ISSUE)
        fail_again(machine)
    assert machine.route(C.SUBSTANTIVE_ISSUE).transition.state is S.STALLED

    resumed = machine.apply(A.AUTHORISE_REWRITE, actor=ActorType.USER)
    assert resumed.state is S.SUBSTANTIVE_REWRITING
    assert machine.ledger.spent(LimitKind.SUBSTANTIVE) == 4
    assert machine.ledger.approved(LimitKind.SUBSTANTIVE) == 1


def test_reopening_the_architecture_by_hand_spends_an_architecture_round() -> None:
    machine = machine_at(S.ARCHITECTURE_APPROVED)
    machine.apply(A.REOPEN_ARCHITECTURE, actor=ActorType.USER)
    assert machine.ledger.spent(LimitKind.ARCHITECTURE) == 1
    assert machine.ledger.approved(LimitKind.ARCHITECTURE) == 1


def test_rounds_never_exceed_the_allowance_they_were_granted() -> None:
    """The invariant the property test generalises: spent ≤ limit + approvals."""
    machine = machine_at(S.REVISION_REQUIRED)
    for _ in range(6):
        machine.route(C.SUBSTANTIVE_ISSUE, approval=RewriteApproval(approved_by="ada"))
        fail_again(machine)
    for kind in LimitKind:
        assert machine.ledger.spent(kind) <= machine.ledger.allowance(kind)


def test_an_escalated_route_reports_what_the_person_must_choose_between() -> None:
    """A stalled run that cannot say why it stalled is a dead end, not a pause."""
    machine = machine_at(S.REVISION_REQUIRED)
    for _ in range(3):
        machine.route(C.SUBSTANTIVE_ISSUE)
        fail_again(machine)
    result = machine.route(C.SUBSTANTIVE_ISSUE)
    assert result.reason
    assert set(machine.available_actions()) >= {
        A.AUTHORISE_REWRITE,
        A.RETURN_TO_BRIEF,
        A.REOPEN_ARCHITECTURE,
        A.OVERRIDE_AND_APPROVE,
    }


def test_a_route_the_policy_forbids_is_refused_before_the_machine_moves() -> None:
    machine = machine_at(S.REVISION_REQUIRED)
    with pytest.raises(WorkflowError):
        machine.route(C.STYLE_ISSUE, prefer=S.SOURCE_MODEL_EXTRACTING)
    assert machine.state is S.REVISION_REQUIRED
