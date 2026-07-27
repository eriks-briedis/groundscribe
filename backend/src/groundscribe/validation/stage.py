"""The final-validation stage (phase 08).

plan/08 → *ValidateFinalOutput*: run the deterministic checks over the version
that passed review, then do exactly one of four things — pass it, apply safe
mechanical corrections and pass it, fail it, or send it back to the stage that can
fix it. Never creatively rewrite.

The stage calls no model. Every other stage in the pipeline asks one for
judgement; this one is the gate that judgement has to get past, and a validator
that could rephrase could also introduce. Its whole output is a function of its
input, which is what lets an author re-run a failure and see the same answer.

Corrections produce a **new version branched from the one that was checked**, not
an edit of it. Nothing in this system overwrites an artefact, and a correction
applied in place would leave the validation report describing a version that no
longer exists. The report names the version it checked; the corrected version
names it as parent; both are readable afterwards.

The report lists every check that *ran*, not only the ones that objected. A
validator that quietly stopped performing a check would otherwise be
indistinguishable from an article that kept passing it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.drafting import store_version
from groundscribe.stages.schemas import ArticleBriefDocument, ArticleDraft, SourceModel
from groundscribe.validation.checks import (
    SafeCorrection,
    ValidationCheck,
    ValidationFinding,
    ValidationInput,
    run_checks,
)
from groundscribe.workflow.policy import FailureCategory
from groundscribe.workflow.states import WorkflowAction

#: The stage name and routing key. No prompt template: this stage calls no model.
VALIDATION_STAGE = "validate_article"


class ValidationReportDocument(BaseModel):
    """The stored report: what was checked, what objected, and what was fixed."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    validator_version: str
    passed: bool
    checks_run: tuple[str, ...]
    findings: tuple[dict[str, object], ...] = ()
    corrections: tuple[dict[str, str], ...] = ()


class CorrectedVersion:
    """The article after safe corrections, and the version it was stored as."""

    __slots__ = ("draft", "snapshot", "version")

    def __init__(
        self,
        *,
        draft: ArticleDraft,
        version: domain_models.ArticleVersion,
        snapshot: ArtifactSnapshot,
    ) -> None:
        self.draft = draft
        self.version = version
        self.snapshot = snapshot


class ValidationOutcome:
    """The verdict, the report, the corrections, and where the run should go."""

    __slots__ = ("category", "corrected", "corrections", "findings", "passed", "report", "row")

    def __init__(
        self,
        *,
        report: ValidationReportDocument,
        row: domain_models.ValidationReport,
        findings: tuple[ValidationFinding, ...],
        corrections: tuple[SafeCorrection, ...],
        corrected: CorrectedVersion | None,
        passed: bool,
        category: FailureCategory | None,
    ) -> None:
        self.report = report
        self.row = row
        self.findings = findings
        self.corrections = corrections
        self.corrected = corrected
        self.passed = passed
        self.category = category


class ValidateFinalOutput:
    """Check the finished article against everything it promised to be."""

    name: ClassVar[str] = VALIDATION_STAGE
    impl_version: ClassVar[str] = "1.0"

    #: The entry edge was taken by whoever moved the run into validation; the exit
    #: depends on what the checks found.
    entry_action: ClassVar[WorkflowAction | None] = None
    exit_action: ClassVar[WorkflowAction | None] = None

    def __init__(
        self,
        *,
        draft: ArticleDraft,
        version: domain_models.ArticleVersion,
        version_snapshot: ArtifactSnapshot,
        passed_version: domain_models.ArticleVersion,
        brief: ArticleBriefDocument,
        source_model: SourceModel,
        concept: domain_models.ArticleConcept | None = None,
        prohibited_terms: Sequence[str] = (),
        transitions: bool = True,
    ) -> None:
        self._draft = draft
        self._version = version
        self._version_snapshot = version_snapshot
        self._passed_version = passed_version
        self._brief = brief
        self._source_model = source_model
        self._concept = concept
        self._prohibited_terms = tuple(prohibited_terms)
        self._transitions = transitions

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[ValidationOutcome]:
        """Run every check, correct what is mechanically correctable, record the rest."""
        context.recorder.record_input(execution, self._version_snapshot, role="article_version")
        findings = run_checks(self._input(context))

        corrections = tuple(
            finding.correction for finding in findings if finding.correction is not None
        )
        remaining = tuple(finding for finding in findings if finding.correction is None)
        corrected = self._apply(context, execution, corrections) if corrections else None

        passed = not remaining
        report = ValidationReportDocument(
            validator_version=self.impl_version,
            passed=passed,
            checks_run=tuple(check.value for check in ValidationCheck),
            findings=tuple(_as_payload(finding) for finding in remaining),
            corrections=tuple(
                {
                    "before": correction.before,
                    "after": correction.after,
                    "reason": correction.reason,
                }
                for correction in corrections
            ),
        )
        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.VALIDATION_REPORT,
            content=report.model_dump(mode="json"),
            role="validation_report",
        )
        row = self._store(context, execution, snapshot, passed=passed)
        category = _category_for(remaining)
        self._record_decision(context, execution, report, category)

        # The article version travels with the result so the exit edge carries it:
        # phase 05's `validation_passed` is what marks a version as the one that
        # may be exported, and it can only mark an artefact it was handed. The
        # corrected version when there is one — approving the text that was
        # checked rather than the text that will be published would defeat the
        # point of checking it.
        published = corrected.snapshot if corrected is not None else self._version_snapshot
        outputs: tuple[ArtifactSnapshot, ...] = (published, snapshot)

        return StageResult(
            value=ValidationOutcome(
                report=report,
                row=row,
                findings=remaining,
                corrections=corrections,
                corrected=corrected,
                passed=passed,
                category=category,
            ),
            outputs=outputs,
            exit_action=self._exit_for(passed),
            detail={
                "passed": passed,
                "findings": len(remaining),
                "corrections": len(corrections),
                "category": category.value if category is not None else None,
            },
        )

    def _input(self, context: PipelineContext) -> ValidationInput:
        """What the checks read, including the two facts only the stage knows."""
        return ValidationInput(
            draft=self._draft,
            brief=self._brief,
            source_text=self._source_model.model_dump_json(),
            constraints=context.constraints,
            prohibited_terms=self._prohibited_terms,
            version_id=self._version.id,
            passed_version_id=self._passed_version.id,
            hash_verified=context.snapshots.verify(self._version_snapshot),
        )

    def _apply(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        corrections: Sequence[SafeCorrection],
    ) -> CorrectedVersion:
        """Apply the safe corrections and store the result as a new version.

        A new version rather than an edit, for the reason every other stage in the
        pipeline creates one: the artefact that was checked has to stay readable
        beside the report that checked it.
        """
        body = self._draft.body
        for correction in corrections:
            body = body.replace(correction.before, correction.after)
        corrected = self._draft.model_copy(update={"body": body})

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.ARTICLE_VERSION,
            content=corrected.model_dump(mode="json"),
            role="article_version",
            parent=self._version_snapshot,
        )
        if self._concept is not None:
            _, version = store_version(
                context,
                execution,
                corrected,
                snapshot,
                concept=self._concept,
                parent=self._version,
            )
        else:
            version = _version_row(context, execution, snapshot, parent=self._version)
        return CorrectedVersion(draft=corrected, version=version, snapshot=snapshot)

    def _store(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        snapshot: ArtifactSnapshot,
        *,
        passed: bool,
    ) -> domain_models.ValidationReport:
        """The report as a row, against the version it checked."""
        row = domain_models.ValidationReport(
            id=uuid.uuid4().hex,
            article_version_id=self._version.id,
            passed=passed,
            snapshot_id=snapshot.id,
            created_by_execution_id=execution.id,
        )
        context.session.add(row)
        context.session.flush()
        return row

    def _record_decision(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        report: ValidationReportDocument,
        category: FailureCategory | None,
    ) -> models.DecisionRecord:
        """Why this article may or may not be published, in reviewable form."""
        return context.recorder.record_decision(
            execution,
            decision_type="final_validation",
            decided_by=VALIDATION_STAGE,
            decided_by_type=ActorType.POLICY,
            policy_version=self.impl_version,
            inputs={
                "checks_run": list(report.checks_run),
                "findings": [str(finding.get("detail", "")) for finding in report.findings],
                "corrections": len(report.corrections),
                "category": category.value if category is not None else None,
            },
            outcome="passed" if report.passed else "failed",
            rationale=(
                "every check is a predicate over the article and its brief; this stage "
                "calls no model, so the same inputs give the same answer to anyone who re-runs it"
            ),
        )

    def _exit_for(self, passed: bool) -> WorkflowAction | None:
        if not self._transitions:
            return None
        return WorkflowAction.VALIDATION_PASSED if passed else WorkflowAction.VALIDATION_FAILED


def _as_payload(finding: ValidationFinding) -> dict[str, object]:
    """One finding in the shape the stored report keeps it."""
    return {
        "check": finding.check.value,
        "detail": finding.detail,
        "severity": finding.severity.value,
        "passage": finding.passage,
        "suggested_route": (
            finding.suggested_route.value if finding.suggested_route is not None else None
        ),
    }


def _category_for(findings: Sequence[ValidationFinding]) -> FailureCategory | None:
    """Where a failed validation sends the run.

    The first finding that names a route wins, and the checks run in the order
    they are declared — fidelity before style — so the most serious destination
    is the one taken. A failure with no route at all (a hash mismatch, the wrong
    version) has no correcting stage: no edit fixes it, and it needs a person.
    """
    for finding in findings:
        if finding.suggested_route is not None:
            return finding.suggested_route
    return None


def _version_row(
    context: PipelineContext,
    execution: models.StageExecution,
    snapshot: ArtifactSnapshot,
    *,
    parent: domain_models.ArticleVersion,
) -> domain_models.ArticleVersion:
    """A corrected version under the parent's article, when no concept was supplied.

    The validator is handed a version rather than a concept, and inventing an
    article row from a title would scatter one article across several identities
    (the reason :func:`store_version` keys on the concept). Reusing the parent's
    article id is the same decision reached from the other end.
    """
    version = domain_models.ArticleVersion(
        id=uuid.uuid4().hex,
        article_id=parent.article_id,
        ordinal=parent.ordinal + 1,
        snapshot_id=snapshot.id,
        created_by_execution_id=execution.id,
        parent_id=parent.id,
    )
    context.session.add(version)
    context.session.flush()
    return version


__all__ = [
    "VALIDATION_STAGE",
    "CorrectedVersion",
    "ValidateFinalOutput",
    "ValidationOutcome",
    "ValidationReportDocument",
]
