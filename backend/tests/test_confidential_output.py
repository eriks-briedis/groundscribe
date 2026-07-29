"""Excluded material cannot appear in the published article (phase 13).

Spec (plan/13 → test-first specification, *Confidential blocked from output*):
material flagged confidential / excluded-from-final-output cannot appear in the
publishable or exported article; final validation fails if it does.

Two gates, on purpose. Final validation is the one that gives a person something
to act on — a named finding, before approval, routed back to the stage that can
fix it. The export guard is the one that is not allowed to be forgotten: it sits
on the transition itself, so no path to publication skips it, including the ones
added after this phase.

**What the check can and cannot do.** It looks for restricted material appearing
*verbatim*. A paraphrase gets through, and that is stated rather than papered
over: this validator calls no model (plan/08), every finding it raises has to be
one an author can reproduce by reading, and a fuzzy matcher would produce
failures nobody could confirm while still missing the determined case. Verbatim
reuse is the failure that actually happens — a sentence copied out of the source
while drafting — and it is the one a deterministic check can catch honestly.

The length floor is what keeps the check usable. Restricting a sentence must not
restrict the words in it: an article that could not say "the cache was cold"
because those words appear in a confidential paragraph would fail every time.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.confidentiality import Confidentiality, Exclusion
from groundscribe.domain.enums import ClaimClassification, SegmentKind
from groundscribe.privacy.material import MINIMUM_SPAN, restricted_spans
from groundscribe.stages.base import StageRunner
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.validation.checks import ValidationCheck
from groundscribe.validation.stage import ValidateFinalOutput
from groundscribe.workflow.states import WorkflowAction
from test_drafting import VOICE
from test_final_validation import checks_for, failed, golden_article, sized_brief
from test_scoring import passing_score, score

INTERNAL_SENTENCE = "The invalidation bug was reported by Northwind three weeks before we saw it."

CONFIDENTIAL_SENTENCE = "Northwind threatened to terminate the contract over the outage."


def _document(session: Session, project_id: str) -> domain_models.SourceDocument:
    document = domain_models.SourceDocument(
        id=f"doc-{project_id}",
        project_id=project_id,
        title="Postmortem",
    )
    session.add(document)
    session.flush()
    return document


def _project(session: Session, suffix: str) -> str:
    user = domain_models.User(id=f"user-{suffix}", name="Ada", email="ada@example.com")
    project = domain_models.Project(id=f"proj-{suffix}", user_id=user.id, title="Postmortem")
    session.add_all([user, project])
    session.flush()
    return project.id


def _segment(
    session: Session,
    document: domain_models.SourceDocument,
    ordinal: int,
    text: str,
    *,
    confidentiality: Confidentiality = Confidentiality.PUBLISHABLE,
    excluded: tuple[Exclusion, ...] = (),
) -> domain_models.SourceSegment:
    segment = domain_models.SourceSegment(
        id=f"{document.id}-{ordinal}",
        document_id=document.id,
        ordinal=ordinal,
        text=text,
        kind=SegmentKind.PARAGRAPH,
        confidentiality=confidentiality,
        excluded=list(excluded),
    )
    session.add(segment)
    session.flush()
    return segment


# ---------------------------------------------------------------------------
# What counts as restricted
# ---------------------------------------------------------------------------


def test_only_material_barred_from_the_output_is_restricted(db_session: Session) -> None:
    """The question is the output boundary, not the classification.

    Internal material is publishable-adjacent — it may be sent to a model — and
    is still barred from the article. Reading the classification instead of the
    boundary would let it through.
    """
    project_id = _project(db_session, "spans")
    document = _document(db_session, project_id)
    _segment(db_session, document, 0, "We shipped a read-through cache in March, and it held.")
    _segment(db_session, document, 1, INTERNAL_SENTENCE, confidentiality=Confidentiality.INTERNAL)
    _segment(
        db_session,
        document,
        2,
        CONFIDENTIAL_SENTENCE,
        confidentiality=Confidentiality.CONFIDENTIAL,
    )

    spans = restricted_spans(db_session, project_id)

    assert INTERNAL_SENTENCE in spans
    assert CONFIDENTIAL_SENTENCE in spans
    assert not any("read-through cache in March" in span for span in spans)


def test_a_publishable_span_flagged_out_of_the_output_is_restricted(
    db_session: Session,
) -> None:
    """The explicit flag is enough on its own, with no classification behind it."""
    project_id = _project(db_session, "flagged")
    document = _document(db_session, project_id)
    _segment(db_session, document, 0, INTERNAL_SENTENCE, excluded=(Exclusion.FINAL_OUTPUT,))

    assert INTERNAL_SENTENCE in restricted_spans(db_session, project_id)


def test_a_restricted_claim_counts_as_much_as_a_segment(db_session: Session) -> None:
    """Extraction can narrow a publishable paragraph into a sensitive claim."""
    project_id = _project(db_session, "claims")
    claim = domain_models.SourceClaim(
        id="claim-1",
        project_id=project_id,
        text=CONFIDENTIAL_SENTENCE,
        classification=ClaimClassification.USER_OBSERVATION,
        confidentiality=Confidentiality.CONFIDENTIAL,
    )
    db_session.add(claim)
    db_session.flush()

    assert CONFIDENTIAL_SENTENCE in restricted_spans(db_session, project_id)


def test_a_short_fragment_is_not_restricted(db_session: Session) -> None:
    """Restricting a sentence must not restrict the words in it.

    An article that could not say "the cache was cold" because those words also
    appear in a confidential paragraph would fail every time, and a check that
    fails every time is a check people learn to route around.
    """
    project_id = _project(db_session, "short")
    document = _document(db_session, project_id)
    _segment(
        db_session,
        document,
        0,
        "It broke. " + CONFIDENTIAL_SENTENCE,
        confidentiality=Confidentiality.CONFIDENTIAL,
    )

    spans = restricted_spans(db_session, project_id)

    assert CONFIDENTIAL_SENTENCE in spans
    assert "It broke." not in spans
    assert all(len(span) >= MINIMUM_SPAN for span in spans)


def test_a_project_with_nothing_flagged_restricts_nothing(db_session: Session) -> None:
    """The common case costs nothing and blocks nothing."""
    project_id = _project(db_session, "clean")
    document = _document(db_session, project_id)
    _segment(db_session, document, 0, "We shipped a read-through cache in March, and it held.")

    assert restricted_spans(db_session, project_id) == ()


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_restricted_material_in_the_article_fails_validation() -> None:
    """The finding plan/13 asks for, raised by name."""
    article = golden_article()
    leaked = article.model_copy(update={"body": f"{article.body}\n\n{CONFIDENTIAL_SENTENCE}\n"})

    assert ValidationCheck.EXCLUDED_MATERIAL in failed(
        checks_for(leaked, excluded_material=(CONFIDENTIAL_SENTENCE,))
    )


def test_the_leak_is_never_corrected_away() -> None:
    """No safe correction, ever.

    A correction that deleted the sentence would change the words of the article,
    which plan/08 forbids outright — and would hand back a *passing* article
    silently altered at the last gate. The author has to see this one.
    """
    article = golden_article()
    leaked = article.model_copy(update={"body": f"{article.body}\n\n{CONFIDENTIAL_SENTENCE}\n"})

    findings = [
        finding
        for finding in _findings(leaked, excluded_material=(CONFIDENTIAL_SENTENCE,))
        if finding.check is ValidationCheck.EXCLUDED_MATERIAL
    ]

    assert findings
    assert all(finding.correction is None for finding in findings)


def test_the_finding_does_not_repeat_the_restricted_text() -> None:
    """The report is stored and exported; quoting the leak into it moves the leak.

    Naming what was found is the whole job of a finding, so this one names the
    *source* of the material instead — enough for an author to go and look, and
    nothing that has to be redacted out of the report afterwards.
    """
    article = golden_article()
    leaked = article.model_copy(update={"body": f"{article.body}\n\n{CONFIDENTIAL_SENTENCE}\n"})

    finding = next(
        item
        for item in _findings(leaked, excluded_material=(CONFIDENTIAL_SENTENCE,))
        if item.check is ValidationCheck.EXCLUDED_MATERIAL
    )

    assert CONFIDENTIAL_SENTENCE not in finding.detail
    assert CONFIDENTIAL_SENTENCE not in finding.passage


def test_an_article_that_borrows_nothing_passes() -> None:
    """A project with restricted material still publishes an ordinary article."""
    assert ValidationCheck.EXCLUDED_MATERIAL not in failed(
        checks_for(excluded_material=(CONFIDENTIAL_SENTENCE, INTERNAL_SENTENCE))
    )


def test_the_check_is_listed_among_the_others() -> None:
    """Fifteen now. The report lists what ran, so the vocabulary has to grow."""
    assert ValidationCheck.EXCLUDED_MATERIAL.value == "excluded_material"
    assert ValidationCheck.EXCLUDED_MATERIAL in set(ValidationCheck)


def _findings(article: Any, **overrides: Any) -> tuple[Any, ...]:
    from groundscribe.validation.checks import run_checks

    return run_checks(checks_for(article, **overrides))


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_stage_finds_the_projects_restricted_material_itself(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The stage reads the flags; no caller has to remember to pass them.

    A safety check a caller can forget to enable is a safety check that is off in
    exactly the code path nobody reviewed — so the stage queries the project's
    flagged material rather than accepting it as an argument.
    """
    drafted, _ = await score(db_session, snapshot_store, passing_score())
    document = _document(db_session, drafted.context.project_id)
    _segment(
        db_session,
        document,
        99,
        CONFIDENTIAL_SENTENCE,
        confidentiality=Confidentiality.CONFIDENTIAL,
    )

    article = golden_article()
    leaked = article.model_copy(update={"body": f"{article.body}\n\n{CONFIDENTIAL_SENTENCE}\n"})
    drafted.context.engine.apply(WorkflowAction.VALIDATE_FINAL)
    result = await StageRunner(drafted.context).run(
        ValidateFinalOutput(
            draft=leaked,
            version=drafted.result.value.version,
            version_snapshot=drafted.result.outputs[0],
            passed_version=drafted.result.value.version,
            brief=sized_brief(),
            source_model=drafted.briefed.source_model,
            prohibited_terms=VOICE.prohibited_terms,
        )
    )

    outcome = result.value
    assert not outcome.passed
    assert any(finding.check is ValidationCheck.EXCLUDED_MATERIAL for finding in outcome.findings)
