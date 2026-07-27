"""Final validation: deterministic checks, and the little it may fix (phase 08).

Spec (plan/08 → *ValidateFinalOutput stage*, and the validation-rules tests, one
per check): no confidential names, no prohibited terminology, no unresolved
placeholders, required facts present, no unsupported numbers introduced, title
matches thesis, formatting matches the platform, length in range, valid Markdown,
valid links, no reserved-material leak, exported version == the version that
passed review, artefact matches its recorded content hash, no internal
annotations left. It may pass, apply safe mechanical corrections, fail, or route
back — and never creatively rewrite.

The stage calls no model, and that is the design rather than an economy. This is
the last gate before publication; a validator that could rephrase could also
introduce, and every check here is a predicate over text a person can re-run and
get the same answer from. Where a check *cannot* be made deterministic — whether
the reserved determinism argument was merely stated or actually developed — it is
not attempted here. Substantive review already judges that, and a validator
guessing at it would produce a failure nobody could confirm.

The golden brief targets 1800 words and the golden draft is a 353-word
representative sample, so these tests supply a brief whose target matches the
fixture. Length is a real check; testing it against a fixture that was never
meant to satisfy it would make every other assertion here fail for the wrong
reason.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from golden import golden_json
from groundscribe.domain.enums import ArtifactType, IssueSeverity
from groundscribe.stages.base import StageRunner
from groundscribe.stages.schemas import ArticleBriefDocument, ArticleDraft
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.validation.checks import ValidationCheck, ValidationInput, run_checks
from groundscribe.validation.stage import VALIDATION_STAGE, ValidateFinalOutput, ValidationOutcome
from groundscribe.workflow.policy import FailureCategory
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from stage_helpers import DEFAULT_CONSTRAINTS
from test_drafting import VOICE, Drafted, draft
from test_scoring import passing_score, score


def sized_brief(**overrides: Any) -> ArticleBriefDocument:
    """The golden brief, retargeted at the length of the golden draft."""
    payload = golden_json("brief.json") | {"target_length_words": 353} | overrides
    return ArticleBriefDocument.model_validate(payload)


def golden_article(**overrides: Any) -> ArticleDraft:
    """The golden draft as an article version."""
    return ArticleDraft.model_validate(
        golden_json("draft.json", suite="draft_to_voice") | overrides
    )


def checks_for(
    article: ArticleDraft | None = None,
    *,
    brief: ArticleBriefDocument | None = None,
    **overrides: Any,
) -> ValidationInput:
    """One validation input, with a field varied per test."""
    source_model = golden_json("source_model.json")
    return ValidationInput(
        draft=article if article is not None else golden_article(),
        brief=brief if brief is not None else sized_brief(),
        source_text=str(source_model),
        constraints=DEFAULT_CONSTRAINTS,
        prohibited_terms=VOICE.avoid,
        **overrides,
    )


def failed(article_input: ValidationInput) -> set[ValidationCheck]:
    """Which checks objected to this input."""
    return {finding.check for finding in run_checks(article_input)}


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_the_golden_article_passes_every_check() -> None:
    """Every check has to agree, or the failing tests below prove nothing.

    A validator whose baseline already fails cannot tell "this check works" from
    "this check always fires".
    """
    assert run_checks(checks_for()) == ()


# ---------------------------------------------------------------------------
# One test per check
# ---------------------------------------------------------------------------


def test_a_confidential_name_fails_validation() -> None:
    """The last gate before publication, and the one failure that cannot be undone."""
    constraints = DEFAULT_CONSTRAINTS.model_copy(update={"confidential_names": ("Northwind",)})
    article = golden_article(body=golden_article().body + "\n\nNorthwind ran the migration.\n")

    findings = run_checks(checks_for(article, constraints=constraints))

    assert [finding.check for finding in findings] == [ValidationCheck.CONFIDENTIAL_NAMES]
    assert findings[0].severity is IssueSeverity.BLOCKING
    assert "Northwind" in findings[0].detail


def test_prohibited_terminology_is_caught_case_insensitively() -> None:
    """A phrase the voice profile forbids is still forbidden capitalised."""
    article = golden_article(body="Delve into the cache key.\n" + golden_article().body)

    findings = run_checks(checks_for(article))

    assert [finding.check for finding in findings] == [ValidationCheck.PROHIBITED_TERMINOLOGY]
    assert findings[0].suggested_route is FailureCategory.STYLE_ISSUE


def test_an_unresolved_placeholder_never_reaches_publication() -> None:
    """A marker is a hole the drafter left on purpose; publishing it publishes the hole."""
    body = golden_article().body.replace("120ms", "120ms [TBD: confirm the window]")

    findings = run_checks(checks_for(golden_article(body=body)))

    assert [finding.check for finding in findings] == [ValidationCheck.UNRESOLVED_PLACEHOLDERS]
    assert findings[0].suggested_route is FailureCategory.FACTUAL_GAP


def test_a_declared_unresolved_marker_fails_even_without_a_bracket() -> None:
    """The draft's own declaration counts, whatever the marker happens to look like."""
    marker = "(still checking)"
    body = golden_article().body.replace("on warm cache.", f"on warm cache {marker}.")
    article = golden_article(
        body=body,
        unresolved=[
            {"marker": marker, "question": "Which window?", "blocking": True, "claim_ids": ["c1"]}
        ],
    )

    assert ValidationCheck.UNRESOLVED_PLACEHOLDERS in failed(checks_for(article))


def test_a_missing_required_fact_fails_validation() -> None:
    """plan/08: required facts present. The brief's mandatory sections name them."""
    article = golden_article(claims_used=["c1", "c2", "c5"], qualifications_applied=["c1"])

    findings = run_checks(checks_for(article))

    assert ValidationCheck.REQUIRED_FACTS in {finding.check for finding in findings}
    detail = next(f for f in findings if f.check is ValidationCheck.REQUIRED_FACTS).detail
    assert "c3" in detail and "c4" in detail


def test_a_number_the_source_never_stated_fails_validation() -> None:
    """plan/08: no unsupported numbers introduced.

    The check a validator can actually make about invention: prose can be
    paraphrased past any comparison, but a figure either appears in the source
    material or was made up somewhere between it and here.
    """
    body = golden_article().body.replace("810ms", "1450ms")

    findings = run_checks(checks_for(golden_article(body=body)))

    assert [finding.check for finding in findings] == [ValidationCheck.UNSUPPORTED_NUMBERS]
    assert "1450" in findings[0].detail


def test_a_title_that_shares_nothing_with_the_thesis_fails() -> None:
    """A retitled article promising something the argument does not deliver."""
    article = golden_article(title="Seven habits of effective platform teams")

    assert ValidationCheck.TITLE_MATCHES_THESIS in failed(checks_for(article))


def test_an_article_far_outside_its_target_length_fails() -> None:
    """The brief set a length; three times it is a different article."""
    article = golden_article(body=golden_article().body * 3)

    findings = run_checks(checks_for(article))

    assert ValidationCheck.LENGTH_IN_RANGE in {finding.check for finding in findings}


def test_an_unbalanced_code_fence_is_invalid_markdown() -> None:
    """An unclosed fence swallows the rest of the article on every renderer."""
    body = golden_article().body + "\n```python\nprint('unterminated')\n"

    findings = run_checks(checks_for(golden_article(body=body)))

    assert ValidationCheck.VALID_MARKDOWN in {finding.check for finding in findings}


def test_a_link_with_no_target_fails_validation() -> None:
    """plan/08: valid links and references."""
    body = golden_article().body + "\n\nSee [the follow-up]() for the determinism argument.\n"

    findings = run_checks(checks_for(golden_article(body=body)))

    assert [finding.check for finding in findings] == [ValidationCheck.VALID_LINKS]


def test_reserved_material_appearing_verbatim_fails_validation() -> None:
    """The brief reserved it; the article printed it.

    Verbatim only. Whether the reserved argument was *developed* rather than
    merely stated is a judgement, substantive review makes it, and a deterministic
    validator claiming to have made it would be producing failures nobody could
    check.
    """
    reserved = sized_brief().reserved_material[0]
    article = golden_article(body=golden_article().body + f"\n\n{reserved}\n")

    assert ValidationCheck.RESERVED_MATERIAL in failed(checks_for(article))


def test_an_internal_annotation_left_in_the_prose_fails_validation() -> None:
    """plan/08: no trace-only or source-only annotations remain."""
    body = golden_article().body.replace("810ms", "810ms <!-- from c1, warm cache -->")

    findings = run_checks(checks_for(golden_article(body=body)))

    assert ValidationCheck.INTERNAL_ANNOTATIONS in {finding.check for finding in findings}


def test_a_heading_level_that_skips_is_a_formatting_problem() -> None:
    """plan/08: formatting matches the platform. A skipped level breaks every outline."""
    body = golden_article().body.replace("## The key that named", "#### The key that named")

    findings = run_checks(checks_for(golden_article(body=body)))

    assert [finding.check for finding in findings] == [ValidationCheck.PLATFORM_FORMATTING]


def test_the_checks_are_exactly_the_ones_the_spec_lists() -> None:
    """Fourteen, and a validator quietly missing one is a validator that passes it."""
    assert {check.value for check in ValidationCheck} == {
        "confidential_names",
        "prohibited_terminology",
        "unresolved_placeholders",
        "required_facts",
        "unsupported_numbers",
        "title_matches_thesis",
        "platform_formatting",
        "length_in_range",
        "valid_markdown",
        "valid_links",
        "reserved_material",
        "internal_annotations",
        "exported_version",
        "content_hash",
    }


# ---------------------------------------------------------------------------
# Safe mechanical corrections
# ---------------------------------------------------------------------------


def test_a_safe_correction_is_applied_rather_than_failed() -> None:
    """plan/08: apply safe mechanical corrections — and never creatively rewrite.

    The test of "safe" is that the correction changes no word of the prose. A
    skipped heading level is renumbering; an internal annotation is text that was
    never part of the article. Both are reversible and neither needs judgement.
    """
    body = golden_article().body.replace("## The key that named", "#### The key that named")
    findings = run_checks(checks_for(golden_article(body=body)))

    (finding,) = findings
    assert finding.correction is not None
    assert finding.correction.after.startswith("## The key that named")
    assert finding.correction.before.startswith("#### The key that named")


def test_a_correction_never_touches_the_words_of_the_article() -> None:
    """Strip the annotation, keep every word around it."""
    body = golden_article().body.replace("810ms", "810ms <!-- from c1, warm cache -->")
    (finding,) = [
        f
        for f in run_checks(checks_for(golden_article(body=body)))
        if f.check is ValidationCheck.INTERNAL_ANNOTATIONS
    ]

    assert finding.correction is not None
    assert finding.correction.after == ""
    assert "<!--" in finding.correction.before


def test_a_failure_with_no_correction_offers_none() -> None:
    """Nothing mechanical fixes a number the source never contained."""
    body = golden_article().body.replace("810ms", "1450ms")

    (finding,) = run_checks(checks_for(golden_article(body=body)))

    assert finding.correction is None


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


async def validate(
    db_session: Session,
    snapshot_store: SnapshotStore,
    **kwargs: Any,
) -> tuple[Drafted, Any]:
    """Score the golden article to a pass, then validate the version that passed."""
    drafted, scored = await score(db_session, snapshot_store, passing_score())
    drafted.context.engine.apply(WorkflowAction.VALIDATE_FINAL)
    result = await StageRunner(drafted.context).run(
        ValidateFinalOutput(
            draft=kwargs.pop("draft", drafted.result.value.draft),
            version=drafted.result.value.version,
            version_snapshot=drafted.result.outputs[0],
            passed_version=drafted.result.value.version,
            brief=kwargs.pop("brief", sized_brief()),
            source_model=drafted.briefed.source_model,
            prohibited_terms=VOICE.avoid,
            **kwargs,
        )
    )
    return drafted, result


async def test_a_clean_article_passes_validation_and_waits_for_a_person(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/05's table: validation passes into human approval, never into export."""
    drafted, result = await validate(db_session, snapshot_store)
    outcome = result.value

    assert isinstance(outcome, ValidationOutcome)
    assert outcome.passed is True
    assert outcome.report.findings == ()
    assert drafted.context.engine.state is WorkflowState.HUMAN_APPROVAL_REQUIRED
    assert drafted.context.engine.validated_version is not None


async def test_the_report_is_stored_against_the_version_it_checked(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A validation nobody can look up afterwards is a validation nobody can trust."""
    drafted, result = await validate(db_session, snapshot_store)
    execution = result.execution

    assert execution is not None
    assert result.value.row.article_version_id == drafted.result.value.version.id
    assert result.value.row.passed is True
    (snapshot,) = [s for s in result.outputs if s.artifact_type is ArtifactType.VALIDATION_REPORT]
    assert snapshot_store.verify(snapshot) is True
    assert result.value.row.created_by_execution_id == execution.id
    # Every check that ran is named, not only the ones that objected.
    assert set(result.value.report.checks_run) == {check.value for check in ValidationCheck}


async def test_a_failing_validation_returns_the_run_to_revision(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/05: validation is deterministic; a failure re-enters routing, never export."""
    body = golden_article().body.replace("810ms", "1450ms")
    drafted, result = await validate(db_session, snapshot_store, draft=golden_article(body=body))

    assert result.value.passed is False
    assert result.value.category is FailureCategory.FACTUAL_GAP
    assert drafted.context.engine.state is WorkflowState.REVISION_REQUIRED
    assert drafted.context.engine.validated_version is None


async def test_a_correctable_problem_is_corrected_and_stored_as_a_new_version(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: apply safe mechanical corrections. The corrected text is a version.

    Not an edit of the one that was checked. Nothing in this system overwrites an
    artefact, and a corrected article that replaced its parent in place would make
    the validation report describe a version that no longer exists.
    """
    body = golden_article().body.replace("## The key that named", "#### The key that named")
    drafted, result = await validate(db_session, snapshot_store, draft=golden_article(body=body))
    outcome = result.value

    assert outcome.passed is True
    assert outcome.corrections
    assert outcome.corrected is not None
    assert "#### The key that named" not in outcome.corrected.draft.body
    assert "## The key that named" in outcome.corrected.draft.body
    assert outcome.corrected.version.parent_id == drafted.result.value.version.id
    # The words are untouched: only the markup moved.
    assert outcome.corrected.draft.thesis == golden_article().thesis
    assert drafted.context.engine.state is WorkflowState.HUMAN_APPROVAL_REQUIRED


async def test_validating_a_version_other_than_the_one_that_passed_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: exported version == the version that passed review.

    The whole chain of checks is about one specific version, so validating a
    different one would be a report that says "checked" about text nobody checked.
    """
    drafted, scored = await score(db_session, snapshot_store, passing_score())
    drafted.context.engine.apply(WorkflowAction.VALIDATE_FINAL)
    other = await draft(db_session, snapshot_store)

    result = await StageRunner(drafted.context).run(
        ValidateFinalOutput(
            draft=drafted.result.value.draft,
            version=drafted.result.value.version,
            version_snapshot=drafted.result.outputs[0],
            passed_version=other.result.value.version,
            brief=sized_brief(),
            source_model=drafted.briefed.source_model,
            prohibited_terms=VOICE.avoid,
        )
    )

    assert result.value.passed is False
    assert ValidationCheck.EXPORTED_VERSION in {
        finding.check for finding in result.value.report.findings
    }


async def test_a_tampered_snapshot_fails_the_hash_check(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: the artefact matches its recorded content hash.

    The one check that is not about the article at all. Everything else asks
    whether the text is publishable; this asks whether the text is the text that
    was approved, which no amount of reading it would reveal.
    """
    drafted, scored = await score(db_session, snapshot_store, passing_score())
    drafted.context.engine.apply(WorkflowAction.VALIDATE_FINAL)
    snapshot = drafted.result.outputs[0]
    snapshot.content_hash = "0" * 64
    db_session.flush()

    result = await StageRunner(drafted.context).run(
        ValidateFinalOutput(
            draft=drafted.result.value.draft,
            version=drafted.result.value.version,
            version_snapshot=snapshot,
            passed_version=drafted.result.value.version,
            brief=sized_brief(),
            source_model=drafted.briefed.source_model,
            prohibited_terms=VOICE.avoid,
        )
    )

    assert result.value.passed is False
    assert ValidationCheck.CONTENT_HASH in {
        finding.check for finding in result.value.report.findings
    }


async def test_the_validator_calls_no_model(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: deterministic. Nothing that can rephrase can be trusted not to.

    Asserted rather than assumed: the stage runs through the same runner as every
    other, and the difference between "does not call a model" and "usually does
    not" is exactly the difference between a validator and a reviewer.
    """
    _, result = await validate(db_session, snapshot_store)
    execution = result.execution

    assert execution is not None
    assert execution.model_invocations == []
    assert result.usage.total_tokens == 0
    assert result.value.report.validator_version == ValidateFinalOutput.impl_version
    assert VALIDATION_STAGE == "validate_article"
