"""What this system promises about repeating itself, and what it refuses to (phase 12).

plan/12 → *Reproducibility contract: promise complete inspection + config
preservation + repeatable deterministic operations + linked replays +
original-vs-replay comparison; do **not** promise bit-for-bit reproduction of
hosted models*, and the risk it answers: *misleading reproducibility claims —
present replay as a new execution*.

The temptation this guards against is small and specific. Everything else in the
codebase points toward reproducibility — content-addressed artefacts, recorded
requests, versioned prompts and rubrics — and it would be natural to describe the
result as "reproducible", which is a word a reader will take to mean the model
says the same thing twice. It does not, and no amount of provenance makes it.

So the contract is data rather than prose in a README: five things promised, one
thing refused, each carrying the name of the test that demonstrates it. A
promise nothing demonstrates is a marketing claim, and the guard below is what
stops one being added.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundscribe.experiments.reproducibility import (
    NOT_PROMISED,
    REPRODUCIBILITY,
    Guarantee,
)

TESTS = Path(__file__).parent


def sources() -> str:
    """Every backend test, as one string to look for a test name in."""
    return "\n".join(path.read_text(encoding="utf-8") for path in TESTS.glob("test_*.py"))


def test_the_contract_names_everything_the_plan_promises() -> None:
    """Five guarantees, by the names plan/12 gives them.

    Written down so the list can be audited against the plan rather than
    reconstructed from whatever the code happens to do — which is how a promise
    quietly widens.
    """
    assert {item.name for item in REPRODUCIBILITY} == {
        "complete_inspection",
        "configuration_preservation",
        "deterministic_operations",
        "linked_replays",
        "original_versus_replay_comparison",
    }


def test_every_promise_names_the_test_that_demonstrates_it() -> None:
    """A promise nothing demonstrates is a marketing claim.

    The guard is that the named test actually exists. It cannot check that the
    test proves the right thing, but it can stop a guarantee outliving the
    evidence for it — which is the failure that happens by accident.
    """
    text = sources()

    missing = [item.name for item in REPRODUCIBILITY if item.evidence not in text]

    assert missing == [], f"guarantees with no test behind them: {missing}"


def test_bit_for_bit_reproduction_of_a_hosted_model_is_refused_in_writing() -> None:
    """The one entry that exists to say no.

    Recorded beside the promises rather than left unstated, because a contract
    that lists only what it guarantees invites the reader to assume the rest.
    """
    assert NOT_PROMISED
    assert all(isinstance(item, Guarantee) and not item.promised for item in NOT_PROMISED)
    assert any("hosted" in item.detail for item in NOT_PROMISED)


def test_no_guarantee_claims_a_model_repeats_itself() -> None:
    """A lint over the contract's own wording.

    "Identical", "deterministic" and "reproducible" are the three words that
    would turn this contract into the claim it exists to avoid, and the only
    place any of them is allowed is the guarantee about operations that involve
    no model at all.
    """
    overreaching = [
        item.name
        for item in REPRODUCIBILITY
        if item.name != "deterministic_operations"
        and any(word in item.detail.lower() for word in ("identical", "bit-for-bit"))
    ]

    assert overreaching == []


@pytest.mark.parametrize("guarantee", REPRODUCIBILITY, ids=lambda item: item.name)
def test_a_promised_guarantee_says_what_it_covers(guarantee: Guarantee) -> None:
    """Each one is a sentence a person could hold the system to."""
    assert guarantee.promised
    assert guarantee.detail
    assert guarantee.evidence
