"""Confidentiality flags on source material (phase 13).

Spec (plan/13 → Deliverables, *Confidentiality flags*): source claims and segments
carry publishable / internal / confidential / excluded-from-model-input /
excluded-from-final-output / excluded-from-exported-traces, enforced at final
validation and at export.

The six names are two different things and the tests below insist on the
distinction, because collapsing them is the mistake that makes the whole feature
unenforceable. *Publishable*, *internal* and *confidential* are one classification
— a span is exactly one of them. The three ``excluded-from-…`` names are
independent switches, and a span may carry any combination of them.

What ties them together is that a classification *implies* exclusions and an
explicit flag may only add. If a flag could subtract, "confidential" would be a
label rather than a rule: something could be marked confidential and then, three
edits later, quietly cleared for the model by a second flag nobody read.
"""

from __future__ import annotations

from enum import StrEnum

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.confidentiality import (
    Confidentiality,
    ConfidentialityFlags,
    Exclusion,
)
from groundscribe.domain.enums import ClaimClassification, SegmentKind, SourceFormat


def test_the_six_flags_plan_13_names() -> None:
    """Exactly the vocabulary the spec lists, split along the axis that matters."""
    assert issubclass(Confidentiality, StrEnum)
    assert issubclass(Exclusion, StrEnum)
    assert {c.value for c in Confidentiality} == {"publishable", "internal", "confidential"}
    assert {e.value for e in Exclusion} == {
        "excluded_from_model_input",
        "excluded_from_final_output",
        "excluded_from_exported_traces",
    }


def test_publishable_is_the_default_and_excludes_nothing() -> None:
    """Material nobody has said anything about is ordinary material."""
    flags = ConfidentialityFlags()
    assert flags.classification is Confidentiality.PUBLISHABLE
    assert flags.exclusions == frozenset()
    assert flags.may_be_sent_to_a_provider
    assert flags.may_be_published
    assert flags.may_be_exported_in_traces


def test_confidential_implies_all_three_exclusions() -> None:
    """The strongest classification needs no second flag to be enforceable.

    A person marking a passage confidential has said everything they intend to
    say about it. Requiring them to also tick three exclusion boxes would mean a
    passage marked confidential and nothing else was still fair game.
    """
    flags = ConfidentialityFlags(classification=Confidentiality.CONFIDENTIAL)
    assert flags.exclusions == frozenset(Exclusion)
    assert not flags.may_be_sent_to_a_provider
    assert not flags.may_be_published
    assert not flags.may_be_exported_in_traces


def test_internal_may_be_reasoned_over_but_never_published() -> None:
    """Internal material informs the article without appearing in it.

    This is the whole reason *internal* exists as a third classification rather
    than as a shade of confidential: a postmortem's internal detail is what makes
    the public write-up accurate, and a system that could not send it to the model
    would produce a worse article for no gain in safety.
    """
    flags = ConfidentialityFlags(classification=Confidentiality.INTERNAL)
    assert flags.exclusions == frozenset({Exclusion.FINAL_OUTPUT})
    assert flags.may_be_sent_to_a_provider
    assert not flags.may_be_published
    assert flags.may_be_exported_in_traces


def test_an_explicit_exclusion_adds_to_the_implied_ones() -> None:
    """Publishable material can still be withheld from a provider."""
    flags = ConfidentialityFlags(excluded={Exclusion.MODEL_INPUT})
    assert flags.classification is Confidentiality.PUBLISHABLE
    assert flags.exclusions == frozenset({Exclusion.MODEL_INPUT})
    assert not flags.may_be_sent_to_a_provider
    assert flags.may_be_published


def test_an_explicit_flag_cannot_subtract_an_implied_one() -> None:
    """Restating a subset of what confidential implies changes nothing.

    The resolution is a union, never a replacement. There is no supported way to
    say "confidential, but do send it" — that request is a contradiction, and the
    place to resolve it is the classification, in one edit a reader can see.
    """
    flags = ConfidentialityFlags(
        classification=Confidentiality.CONFIDENTIAL,
        excluded={Exclusion.FINAL_OUTPUT},
    )
    assert flags.exclusions == frozenset(Exclusion)


def test_flags_are_hashable_values() -> None:
    """A value object, so two spans flagged the same way compare equal.

    Ingestion derives a segment's flags from its document's; equality is how a
    caller checks whether anything actually changed without re-reading the row.
    """
    assert ConfidentialityFlags(classification=Confidentiality.INTERNAL) == (
        ConfidentialityFlags(classification=Confidentiality.INTERNAL)
    )
    assert len({ConfidentialityFlags(), ConfidentialityFlags()}) == 1


@pytest.mark.parametrize(
    ("classification", "excluded"),
    [
        (Confidentiality.PUBLISHABLE, ()),
        (Confidentiality.INTERNAL, ()),
        (Confidentiality.CONFIDENTIAL, ()),
        (Confidentiality.PUBLISHABLE, (Exclusion.EXPORTED_TRACES,)),
    ],
)
def test_segment_persists_its_flags(
    db_session: Session,
    classification: Confidentiality,
    excluded: tuple[Exclusion, ...],
) -> None:
    """A segment's flags survive a round trip, exclusions included."""
    document = _document(db_session)
    segment = domain_models.SourceSegment(
        id=f"seg-{classification.value}-{len(excluded)}",
        document_id=document.id,
        ordinal=0,
        text="the cache was invalidated by hand",
        kind=SegmentKind.PARAGRAPH,
        confidentiality=classification,
        excluded=list(excluded),
    )
    db_session.add(segment)
    db_session.flush()
    db_session.expire(segment)

    stored = db_session.get(domain_models.SourceSegment, segment.id)
    assert stored is not None
    assert stored.flags == ConfidentialityFlags(classification=classification, excluded=excluded)


def test_claim_persists_its_flags(db_session: Session) -> None:
    """A claim carries the same vocabulary as the segment it was drawn from.

    Extraction can narrow a publishable segment into a claim that names a
    customer. If only segments could be flagged, the only way to withhold that
    claim would be to withhold the paragraph it came from.
    """
    claim = domain_models.SourceClaim(
        id="claim-confidential",
        project_id=_project(db_session).id,
        text="Acme's migration slipped two quarters",
        classification=ClaimClassification.USER_OBSERVATION,
        confidentiality=Confidentiality.CONFIDENTIAL,
    )
    db_session.add(claim)
    db_session.flush()
    db_session.expire(claim)

    stored = db_session.get(domain_models.SourceClaim, claim.id)
    assert stored is not None
    assert stored.flags.classification is Confidentiality.CONFIDENTIAL
    assert not stored.flags.may_be_sent_to_a_provider


def test_stored_material_defaults_to_publishable(db_session: Session) -> None:
    """Rows written before this phase read back as publishable, not as null.

    The column default is what makes the enforcement points total: a check that
    had to handle "no flag" would have a third branch, and the third branch is
    where material leaks.
    """
    document = _document(db_session)
    segment = domain_models.SourceSegment(
        id="seg-default",
        document_id=document.id,
        ordinal=1,
        text="latency fell from 800ms to 120ms",
    )
    db_session.add(segment)
    db_session.flush()

    assert segment.confidentiality is Confidentiality.PUBLISHABLE
    assert segment.excluded == []
    assert segment.flags == ConfidentialityFlags()


def _project(session: Session) -> domain_models.Project:
    """The one project these rows hang off, created once per test session."""
    existing = session.scalars(
        select(domain_models.Project).where(domain_models.Project.id == "proj-conf")
    ).first()
    if existing is not None:
        return existing
    user = domain_models.User(id="user-conf", name="Ada", email="ada@example.com")
    project = domain_models.Project(id="proj-conf", user_id=user.id, title="Cache postmortem")
    session.add_all([user, project])
    session.flush()
    return project


def _document(session: Session) -> domain_models.SourceDocument:
    existing = session.scalars(
        select(domain_models.SourceDocument).where(domain_models.SourceDocument.id == "doc-conf")
    ).first()
    if existing is not None:
        return existing
    document = domain_models.SourceDocument(
        id="doc-conf",
        project_id=_project(session).id,
        title="Postmortem",
        source_format=SourceFormat.MARKDOWN,
    )
    session.add(document)
    session.flush()
    return document
