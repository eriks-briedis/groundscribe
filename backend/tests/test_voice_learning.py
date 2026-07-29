"""Learning a rule from repeated edits, and refusing to apply it (phase 10).

Spec (plan/10 → Test-first specification): *a recurring edit pattern yields a
suggestion, not an automatic profile change; the rule persists only after
explicit approval; the manual edit records its voice-training-eligibility flag.*

The gate is the feature. Detecting that someone keeps replacing one word with
another is the easy half and, on its own, actively harmful: a system that turned
three edits into a permanent rule would keep teaching itself things the author
never agreed to, and each one would be invisible until it showed up in prose.
plan/00 names this directly — *human control at high-leverage decisions* — and a
rule that governs everything you will ever publish is as high-leverage as this
system gets.

So the tests are mostly about what must *not* happen: no profile changes until a
person says so, ineligible edits are not evidence, and a rejected suggestion
leaves a record rather than disappearing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import FindingStatus
from groundscribe.provenance.enums import InterventionType
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.voice.enums import InstructionStrength, VoiceCategory
from groundscribe.voice.learning import (
    EditPattern,
    VoiceLearning,
    detect_edit_patterns,
)
from groundscribe.voice.models import ManualEdit, VoiceSuggestion
from groundscribe.voice.precedence import resolve_voice
from groundscribe.voice.schemas import VoiceInstruction, VoiceProfileDocument
from provenance_helpers import make_recorder, seed_project

AUTHOR = "u1"

#: Three times the author replaced the same word. The pattern plan/10 uses as its
#: example, in the form it actually arrives in: not a labelled preference, just
#: someone fixing the same thing again.
DRAMATIC = (
    ("The results were dramatic.", "The results were a 6x drop."),
    ("A dramatic reduction in retries.", "A 40% reduction in retries."),
    ("Latency dropped dramatic amounts.", "Latency dropped 690ms."),
)


def seed_version(session: Session, recorder: ProvenanceRecorder) -> domain_models.ArticleVersion:
    """The minimum chain a manual edit hangs from."""
    project_id = seed_project(session, user_id=AUTHOR)
    run = recorder.start_run(project_id=project_id)
    execution = recorder.start_stage(run, stage="manual_edit")
    article = domain_models.Article(id="a1", project_id=project_id, title="Caching")
    version = domain_models.ArticleVersion(
        id="v1", article_id="a1", ordinal=0, created_by_execution_id=execution.id
    )
    session.add_all([article, version])
    session.flush()
    return version


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    """One recorder for the whole test: two would run two id counters over one
    database and collide on the first write."""
    return make_recorder(db_session, snapshot_store)


@pytest.fixture
def version(db_session: Session, recorder: ProvenanceRecorder) -> domain_models.ArticleVersion:
    return seed_version(db_session, recorder)


@pytest.fixture
def learning(db_session: Session, recorder: ProvenanceRecorder) -> VoiceLearning:
    return VoiceLearning(db_session, recorder=recorder)


def record_all(
    learning: VoiceLearning,
    version: domain_models.ArticleVersion,
    *,
    eligible: bool = True,
) -> tuple[ManualEdit, ...]:
    return tuple(
        learning.record_edit(
            version=version, before=before, after=after, edited_by=AUTHOR, eligible=eligible
        )
        for before, after in DRAMATIC
    )


PROFILE = VoiceProfileDocument(
    name="ada",
    version="1",
    instructions=(
        VoiceInstruction(
            id="direct",
            category=VoiceCategory.TONE,
            strength=InstructionStrength.TENDENCY,
            text="State the finding, then the evidence.",
        ),
    ),
)


# ----------------------------------------------------------------------
# Manual edits, and what they may be used for
# ----------------------------------------------------------------------


def test_a_manual_edit_records_whether_it_may_teach_anything(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """plan/10 → *manual edits record whether they are eligible as voice-training
    evidence.*

    A flag on the edit rather than a filter applied later, because eligibility is
    a fact about *why* the edit was made — fixing a fact is not a style
    preference — and only the person making it knows which it was.
    """
    edit = learning.record_edit(
        version=version,
        before="p99 fell to 120ms",
        after="p99 fell to 118ms",
        edited_by=AUTHOR,
        eligible=False,
    )

    assert edit.voice_training_eligible is False
    assert edit.made_by == AUTHOR


def test_an_edit_is_recorded_as_a_human_intervention(
    learning: VoiceLearning, version: domain_models.ArticleVersion, db_session: Session
) -> None:
    """plan/03 → a person stepping into a run is a recorded intervention.

    An edit that changed the prose and left no trace would make the article's
    provenance a description of what the *model* did.
    """
    learning.record_edit(
        version=version, before="dramatic", after="6x", edited_by=AUTHOR, eligible=True
    )

    interventions = db_session.scalars(select(domain_models.User)).all()
    recorded = db_session.scalars(
        select(ManualEdit).where(ManualEdit.article_version_id == version.id)
    ).all()

    assert interventions
    assert recorded[0].created_by_execution_id is not None


def test_ineligible_edits_are_not_evidence(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """Correcting a number three times is not a style preference.

    The single most likely way this feature goes wrong: a person fixing facts
    teaches the system a rule about facts.
    """
    record_all(learning, version, eligible=False)

    assert detect_edit_patterns(learning.training_edits(AUTHOR)) == ()


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------


def test_a_word_replaced_again_and_again_is_a_pattern(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """plan/10's own example: dramatic → concrete."""
    record_all(learning, version)

    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))

    assert pattern.removed == "dramatic"
    assert pattern.occurrences == 3
    assert len(pattern.edit_ids) == 3


def test_two_edits_are_not_yet_a_habit(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """The threshold is the difference between a habit and a coincidence.

    Suggesting after two would mean interrupting the author about a word they
    happened to dislike twice, which is how a good feature becomes noise.
    """
    for before, after in DRAMATIC[:2]:
        learning.record_edit(
            version=version, before=before, after=after, edited_by=AUTHOR, eligible=True
        )

    assert detect_edit_patterns(learning.training_edits(AUTHOR)) == ()


def test_unrelated_edits_produce_nothing(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """Three different fixes are three fixes."""
    for before, after in (
        ("dramatic", "6x"),
        ("very fast", "12ms"),
        ("a lot of retries", "40% of retries"),
    ):
        learning.record_edit(
            version=version, before=before, after=after, edited_by=AUTHOR, eligible=True
        )

    assert detect_edit_patterns(learning.training_edits(AUTHOR)) == ()


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------


def test_a_pattern_becomes_a_suggestion_and_nothing_else(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """plan/10 → *do not auto-update the permanent profile*.

    The suggestion is proposed, carries the instruction it would add, and the
    profile it would change is untouched.
    """
    record_all(learning, version)
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))

    suggestion = learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)

    assert suggestion.status is FindingStatus.PROPOSED
    assert suggestion.decided_by == ""
    assert "dramatic" in suggestion.instruction["prohibits"]


def test_a_suggestion_shows_the_edits_behind_it(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """plan/10 → *show supporting examples*.

    A person asked to make something a permanent rule needs to see what it was
    inferred from. "You often replace X" is a claim; three of their own
    sentences are evidence they can disagree with.
    """
    edits = record_all(learning, version)
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))

    suggestion = learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)

    assert set(suggestion.evidence["edit_ids"]) == {edit.id for edit in edits}
    assert suggestion.evidence["examples"][0]["before"] == DRAMATIC[0][0]


def test_an_unapproved_suggestion_changes_no_prose(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """The property that matters, stated where it is observable.

    A suggestion nobody has accepted must not reach the resolver, or the "gate"
    is a label on a thing that already happened.
    """
    record_all(learning, version)
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))
    learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)

    resolved = resolve_voice(global_profile=PROFILE)

    assert [active.instruction.id for active in resolved.active] == ["direct"]


def test_approval_produces_a_new_profile_version(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """plan/10 → *store only after approval*, and plan/00 → never in place.

    The approved rule lands in a *new* version. Editing the old one would make
    every article that recorded "written under ada@1" describe a document that
    no longer exists.
    """
    record_all(learning, version)
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))
    suggestion = learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)

    updated = learning.approve(suggestion, profile=PROFILE, approved_by=AUTHOR, version="2")

    assert updated.version == "2"
    assert [i.id for i in updated.instructions] == ["direct", pattern.instruction_id]
    assert PROFILE.version == "1"
    assert [i.id for i in PROFILE.instructions] == ["direct"]


def test_approval_is_attributed_and_recorded(
    learning: VoiceLearning, version: domain_models.ArticleVersion, db_session: Session
) -> None:
    """Who made a permanent rule permanent is the question this feature invites."""
    record_all(learning, version)
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))
    suggestion = learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)

    learning.approve(suggestion, profile=PROFILE, approved_by=AUTHOR, version="2")

    assert suggestion.status is FindingStatus.ACCEPTED
    assert suggestion.decided_by == AUTHOR
    assert suggestion.decided_at is not None


def test_an_anonymous_approval_is_refused(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """The same rule phases 05 and 06 apply: an unattributed decision is unreviewable."""
    record_all(learning, version)
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))
    suggestion = learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)

    with pytest.raises(ValueError, match="approved_by"):
        learning.approve(suggestion, profile=PROFILE, approved_by="", version="2")


def test_a_rejected_suggestion_is_kept_with_its_reason(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """Rejection is an answer, and the system should not ask again as though it
    had never asked.

    The same reasoning phase 07 applies to dismissed review findings: resolved
    criticism stays visible.
    """
    record_all(learning, version)
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))
    suggestion = learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)

    learning.reject(
        suggestion, rejected_by=AUTHOR, reason="I mean it in the pieces about launches."
    )

    assert suggestion.status is FindingStatus.REJECTED
    assert "launches" in suggestion.reason


def test_a_suggestion_already_decided_cannot_be_decided_again(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """Approving twice would silently produce a third profile version."""
    record_all(learning, version)
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))
    suggestion = learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)
    learning.approve(suggestion, profile=PROFILE, approved_by=AUTHOR, version="2")

    with pytest.raises(ValueError, match="already"):
        learning.approve(suggestion, profile=PROFILE, approved_by=AUTHOR, version="3")


def test_an_approved_rule_arrives_as_a_preference_not_a_rule(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """An inferred instruction is inferred, and it should say so.

    Promoting a guess straight to a hard rule would let three edits stop an
    article. The author can raise it afterwards; the system should not raise it
    for them.
    """
    record_all(learning, version)
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))
    suggestion = learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)

    updated = learning.approve(suggestion, profile=PROFILE, approved_by=AUTHOR, version="2")
    inferred = updated.instructions[-1]

    assert inferred.strength is InstructionStrength.STRONG_PREFERENCE
    assert "3 edits" in inferred.rationale


def test_a_pattern_carries_a_stable_id_for_the_rule_it_would_become() -> None:
    """So approving the same habit twice refines one rule instead of adding two."""
    pattern = EditPattern(removed="dramatic", added="", occurrences=3, edit_ids=("e1", "e2", "e3"))

    assert pattern.instruction_id == "learned-dramatic"


def test_suggestions_are_only_offered_once_per_habit(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """Asking again about something already refused is how a gate becomes nagging."""
    record_all(learning, version)
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))
    first = learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)

    second = learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)

    assert second.id == first.id
    assert (
        len(
            learning.session.scalars(
                select(VoiceSuggestion).where(VoiceSuggestion.user_id == AUTHOR)
            ).all()
        )
        == 1
    )


def test_an_edit_intervention_names_the_edit_it_recorded(
    learning: VoiceLearning, version: domain_models.ArticleVersion
) -> None:
    """The trace links to the row, so provenance and evidence are one chain."""
    edit = learning.record_edit(
        version=version, before="dramatic", after="6x", edited_by=AUTHOR, eligible=True
    )

    interventions = learning.interventions_for(edit)

    assert [i.intervention_type for i in interventions] == [InterventionType.EDIT]
    assert interventions[0].payload["manual_edit_id"] == edit.id
