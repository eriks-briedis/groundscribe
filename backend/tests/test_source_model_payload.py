"""What a stage sends when it sends the source model.

The source model was 57% of everything the run of 2026-08-06 put on the wire —
the same ~62k-character blob, nineteen times — and three separate things were
wrong with how it was spelled. These tests fix each of them in place, because
each is the kind of regression that costs money silently: the pipeline keeps
working, the articles keep scoring, and only the token counter notices.
"""

from __future__ import annotations

import json

import pytest

from groundscribe.domain.enums import ClaimClassification
from groundscribe.stages.payload import claims_in_scope, source_model_payload
from groundscribe.stages.schemas import (
    DevelopmentEvent,
    Evidence,
    ExtractedClaim,
    Lesson,
    PotentialArgument,
    ProductFact,
    PublicationConstraint,
    SourceModel,
)


def build_model() -> SourceModel:
    """Three claims, one per article, with every segment-carrying shape present."""
    return SourceModel(
        summary="A cache change, and what it cost.",
        product_facts=(ProductFact(statement="It ships with SQLite.", segment_ids=("d-1",)),),
        development_history=(
            DevelopmentEvent(ordinal=1, summary="Shipped the cache.", segment_ids=("d-2",)),
        ),
        claims=(
            ExtractedClaim(
                id="c001",
                text="p99 fell from 810ms to 120ms.",
                classification=ClaimClassification.DIRECTLY_SUPPORTED_FACT,
                evidence=(Evidence(segment_ids=("d-3", "d-4"), quote="p99 fell to 120ms"),),
            ),
            ExtractedClaim(
                id="c002",
                text="The team preferred read-through.",
                classification=ClaimClassification.DIRECTLY_SUPPORTED_FACT,
                evidence=(Evidence(segment_ids=("d-5",), quote="we chose read-through"),),
            ),
            ExtractedClaim(
                id="c003",
                text="Deployment tooling was the harder problem.",
                classification=ClaimClassification.INTERPRETATION,
            ),
        ),
        publication_constraints=(
            PublicationConstraint(
                description="Do not name the customer.", reason="NDA", segment_ids=("d-6",)
            ),
        ),
        lessons=(Lesson(statement="Measure before caching.", claim_ids=("c001", "c003")),),
        potential_arguments=(
            PotentialArgument(thesis="Caching is a measurement problem.", claim_ids=("c001",)),
        ),
    )


def test_the_payload_is_json_rather_than_a_python_dict_literal() -> None:
    """``{{ source_model }}`` used to call :func:`str` on a dict.

    Which put a Python literal on the wire — single quotes, ``', '`` separators —
    for a model that reads JSON everywhere else, and tokenizes that punctuation
    worse than the JSON it was imitating.
    """
    payload = source_model_payload(build_model())
    parsed = json.loads(payload)

    assert parsed["summary"] == "A cache change, and what it cost."
    assert "'" not in payload
    # Compact separators, asserted by reconstruction rather than by searching for
    # ``", "`` — which appears inside the prose above, and is content there.
    assert payload == json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)


def test_segment_ids_are_stripped_wherever_they_appear() -> None:
    """19.3% of the measured payload, and nothing downstream reads them.

    They are checked once, by ``check_citations`` at extraction, against the
    segment rows — never against a prompt. Every stage after that paid to ship an
    identifier it had no use for, and the ids are the worst-tokenizing text in
    the document: a 32-character hex hash repeated 527 times in one call.
    """
    payload = source_model_payload(build_model())

    assert "segment_ids" not in payload
    for missing in ("d-1", "d-2", "d-3", "d-4", "d-5", "d-6"):
        assert missing not in payload
    # The quote stays. It is the anchor a fidelity judgement is actually made
    # against, and it is the thing the ids were standing next to, not the ids.
    assert "p99 fell to 120ms" in payload


def test_an_unprojected_payload_keeps_every_claim() -> None:
    """The stages reading the source as a whole still see it whole.

    Gap analysis judges what the source does *not* say and architecture decides
    the allocation everything else is then scoped by — narrowing either would be
    circular.
    """
    payload = json.loads(source_model_payload(build_model()))

    assert [claim["id"] for claim in payload["claims"]] == ["c001", "c002", "c003"]


def test_a_projection_keeps_only_the_claims_named() -> None:
    payload = json.loads(source_model_payload(build_model(), claim_ids={"c001", "c003"}))

    assert [claim["id"] for claim in payload["claims"]] == ["c001", "c003"]
    assert "The team preferred read-through." not in json.dumps(payload)


def test_a_projection_narrows_the_references_to_match() -> None:
    """A lesson pointing at a claim that is no longer there names nothing.

    The statement survives, because the sentence is worth reading even when the
    claims behind it went to a different article; the dangling reference does
    not, because an id the reader cannot resolve is worse than no id.
    """
    payload = json.loads(source_model_payload(build_model(), claim_ids={"c001"}))

    assert payload["lessons"][0]["statement"] == "Measure before caching."
    assert payload["lessons"][0]["claim_ids"] == ["c001"]
    assert payload["potential_arguments"][0]["claim_ids"] == ["c001"]


def test_an_empty_projection_is_a_projection_and_not_a_reset() -> None:
    """``set()`` means "none of them", and ``None`` means "all of them".

    Worth pinning: the two are easy to conflate at a call site, and conflating
    them in the forgiving direction would quietly restore the whole payload.
    """
    assert json.loads(source_model_payload(build_model(), claim_ids=set()))["claims"] == []
    assert len(json.loads(source_model_payload(build_model()))["claims"]) == 3


def test_an_unknown_claim_id_is_ignored_rather_than_raised() -> None:
    """A draft naming a claim the model does not have is a real defect.

    It is ``check_draft``'s to report, with the id in the message. Failing here
    would replace that diagnosis with a traceback from a serialiser, at a point
    where nothing is yet known about which stage went wrong.
    """
    payload = json.loads(source_model_payload(build_model(), claim_ids={"c001", "c999"}))

    assert [claim["id"] for claim in payload["claims"]] == ["c001"]


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ((("c001",), ("c002",)), {"c001", "c002"}),
        ((("c001",), None), {"c001"}),
        ((None, None), set()),
        ((("c001", "c002"), ("c002",)), {"c001", "c002"}),
    ],
)
def test_scope_is_a_union_of_every_contract(
    sources: tuple[tuple[str, ...] | None, ...], expected: set[str]
) -> None:
    """Union, never intersection — and the direction matters.

    A judge shown less than the draft claims to rest on reports the difference as
    an unsupported claim, which routes a ``factual_gap``: the one route in
    ``workflow-policy.yaml`` with no round limit. Getting this backwards would
    not fail a test, it would spend a run.
    """
    assert claims_in_scope(*sources) == expected


def test_projection_is_smaller_than_the_thing_it_projects() -> None:
    """The whole point, asserted rather than assumed."""
    model = build_model()
    whole = source_model_payload(model)
    scoped = source_model_payload(model, claim_ids={"c001"})

    assert len(scoped) < len(whole) < len(str(model.model_dump(mode="json")))
