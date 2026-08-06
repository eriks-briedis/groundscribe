"""The rules the stage schemas enforce on their own (phase 06).

Every stage output carries validators that turn an editorial rule into repair
feedback the model can act on (plan/04's ladder) rather than a stage failure a
person has to diagnose. Those validators are claimed in the module docstrings and
in the commit history; this module is where they are actually exercised.

The diff and the guards in ``stages.override`` are here for the same reason: both
are shared machinery whose *unhappy* paths — a removed key, an operation naming
nothing, a merge of one article — are exactly the paths a caller hits on the day
something has gone wrong.
"""

from __future__ import annotations

import pytest

from golden import golden_json
from groundscribe.domain.enums import ClaimClassification, GapPriority
from groundscribe.stages.diffing import ChangeKind, StructuredDiff, structured_diff
from groundscribe.stages.errors import OverrideRejected
from groundscribe.stages.override import (
    OverrideCommand,
    OverrideOperation,
    apply_overrides,
)
from groundscribe.stages.schemas import (
    ArchitectureProposal,
    ExtractedClaim,
    GapReport,
    SourceGapQuestion,
    SourceModel,
)


def test_a_supported_fact_must_cite_a_passage() -> None:
    """An unevidenced "fact" is an interpretation wearing the wrong label."""
    with pytest.raises(ValueError, match="directly supported fact"):
        ExtractedClaim(
            id="c1",
            text="The cache halved latency.",
            classification=ClaimClassification.DIRECTLY_SUPPORTED_FACT,
        )

    # The same claim, honestly classified, is accepted with no evidence at all.
    interpretation = ExtractedClaim(
        id="c1",
        text="The cache halved latency.",
        classification=ClaimClassification.INTERPRETATION,
    )
    assert interpretation.evidence == ()


def test_claim_ids_must_be_unique_because_things_reference_them() -> None:
    """Lessons and arguments point at claims by id; duplicates make that ambiguous."""
    claim = {
        "id": "c1",
        "text": "p99 fell.",
        "classification": "user_observation",
        "evidence": [],
    }

    with pytest.raises(ValueError, match="repeated: c1"):
        SourceModel.model_validate({"summary": "s", "claims": [claim, dict(claim)]})


def test_a_gap_report_can_be_read_by_priority() -> None:
    """The queue groups by priority; the report answers that without re-filtering."""
    report = GapReport(
        gaps=(
            SourceGapQuestion(
                id="g1", question="q1", why_it_matters="w", priority=GapPriority.BLOCKING
            ),
            SourceGapQuestion(
                id="g2", question="q2", why_it_matters="w", priority=GapPriority.OPTIONAL
            ),
        )
    )

    assert [gap.id for gap in report.by_priority(GapPriority.BLOCKING)] == ["g1"]
    assert [gap.id for gap in report.by_priority(GapPriority.HIGH_VALUE)] == []


def test_duplicate_article_ids_are_refused() -> None:
    """Two articles with one id cannot both be selected, briefed or overridden."""
    payload = golden_json("architecture.json")
    payload["articles"][1]["id"] = "a1"
    payload["series"]["reading_order"] = ["a1", "a1"]

    with pytest.raises(ValueError, match="repeated: a1"):
        ArchitectureProposal.model_validate(payload)


def test_a_diff_reports_added_and_removed_keys_and_items() -> None:
    """Both directions, at both levels: a rebuild adds and drops as often as it edits."""
    diff = structured_diff(
        {"kept": 1, "dropped": 2, "items": [1, 2, 3]},
        {"kept": 1, "added": 3, "items": [1, 9]},
    )
    by_path = {entry.path: entry for entry in diff.entries}

    assert by_path["dropped"].change is ChangeKind.REMOVED
    assert by_path["dropped"].before == 2
    assert by_path["added"].change is ChangeKind.ADDED
    assert by_path["added"].after == 3
    assert by_path["items.1"].change is ChangeKind.CHANGED
    assert by_path["items.2"].change is ChangeKind.REMOVED
    assert "kept" not in by_path


def test_a_diff_of_identical_structures_is_empty_and_counts_itself() -> None:
    """An empty diff is a fact worth stating: the rebuild changed nothing."""
    payload = golden_json("architecture.json")

    unchanged = structured_diff(payload, payload)
    assert unchanged.is_empty
    assert unchanged.counts() == {"added": 0, "removed": 0, "changed": 0}

    changed = structured_diff(payload, {**payload, "competing_theses": []})
    assert not changed.is_empty
    assert sum(changed.counts().values()) == len(changed.entries)


def test_a_diff_over_lists_of_different_lengths_reports_the_tail() -> None:
    """A grown list is additions, not a wholesale replacement."""
    diff = structured_diff([1], [1, 2])

    assert [(entry.path, entry.change) for entry in diff.entries] == [("1", ChangeKind.ADDED)]
    assert StructuredDiff().is_empty


def _proposal() -> ArchitectureProposal:
    return ArchitectureProposal.model_validate(golden_json("architecture.json"))


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (
            OverrideCommand(operation=OverrideOperation.MERGE, article_ids=("a1",)),
            "at least two articles",
        ),
        (
            OverrideCommand(
                operation=OverrideOperation.SPLIT, article_ids=("a1",), new_ids=("a1a",)
            ),
            "exactly two new ids",
        ),
        (
            OverrideCommand(operation=OverrideOperation.REORDER, order=("a1",)),
            "every article exactly once",
        ),
        (
            OverrideCommand(operation=OverrideOperation.REASSIGN_EVIDENCE, article_ids=("a1",)),
            "at least one claim",
        ),
        (
            OverrideCommand(operation=OverrideOperation.RENAME, title="No target"),
            "needs an article to edit",
        ),
    ],
)
def test_an_incoherent_override_is_refused_before_anything_is_written(
    command: OverrideCommand, message: str
) -> None:
    """``apply_overrides`` is pure, so every one of these fails with nothing persisted."""
    with pytest.raises(OverrideRejected, match=message):
        apply_overrides(_proposal(), (command,))


def test_removing_the_recommended_article_re_points_the_decision_and_says_so() -> None:
    """The decision is stated *about* the articles; an edit can invalidate it."""
    reduced, warnings = apply_overrides(
        _proposal(),
        (OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a1",)),),
    )

    assert reduced.decision.selected == "a2"
    assert any(warning.code == "selection_changed" for warning in warnings)
    assert reduced.series.is_series is False


def test_reassigning_shared_evidence_warns_about_the_overlap() -> None:
    """Two articles arguing the same claim will produce two drafts that overlap."""
    _, warnings = apply_overrides(
        _proposal(),
        (
            OverrideCommand(
                operation=OverrideOperation.REASSIGN_EVIDENCE,
                article_ids=("a2",),
                claim_ids=("c3", "c4"),
            ),
        ),
    )

    shared = next(warning for warning in warnings if warning.code == "shared_evidence")
    assert "c3" in shared.message


def test_an_edit_that_trades_nothing_off_produces_no_warning() -> None:
    """Warnings are for trade-offs; a clean edit must not manufacture one.

    A warning on every operation would train the author to dismiss them unread,
    which costs more than the warnings buy.
    """
    proposal = _proposal()

    # Evidence reassigned to claims nothing else argues: no overlap to warn about.
    _, reassigned = apply_overrides(
        proposal,
        (
            OverrideCommand(
                operation=OverrideOperation.REASSIGN_EVIDENCE,
                article_ids=("a2",),
                claim_ids=("c1", "c6"),
            ),
        ),
    )
    assert reassigned == ()

    # And a removal whose claims are still argued elsewhere orphans nothing.
    widened, _ = apply_overrides(
        proposal,
        (
            OverrideCommand(
                operation=OverrideOperation.REASSIGN_EVIDENCE,
                article_ids=("a1",),
                claim_ids=("c2", "c3", "c4", "c5"),
            ),
        ),
    )
    _, removed = apply_overrides(
        widened, (OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a2",)),)
    )
    assert removed == ()
