"""User architecture override, and the lock that follows approval (phase 06 §5).

plan/06 → *User architecture override*: merge, split, remove, reorder, rename,
edit-thesis and reassign-evidence; trade-off warnings surfaced *without blocking*;
versioned locking so an approved architecture cannot change silently; and override
provenance — before/after snapshots, a structured diff, the reason, the warnings
shown and accepted, and a lineage branch.

**Warnings advise; they never veto.** The author knows things the pipeline cannot:
that the removed article is being saved for a talk, that the merged piece is for a
different audience. plan/06 is explicit that trade-offs are surfaced without
blocking, so every warning here is data attached to the record — including the
ones the author did not accept, because "shown and ignored" and "never shown" are
different facts and only one of them is a bug.

**The overrides themselves are a pure function.** ``apply_overrides`` takes a
proposal and returns a new one; nothing is written, nothing is transitioned. That
is what lets a caller (phase 11's editor) show the author the consequences —
warnings, diff, thin-content risk — *before* they commit to them.

**Committing goes back through the machine.** An override reopens the
architecture, which is a person's decision and a user edge, and re-submits the new
version for review. The engine's own guard then requires the new snapshot to fork
from the approved one and to carry an override naming who authorised it; going
around this module cannot produce a silent replacement, which is the point.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType, BranchStatus
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType, InterventionType
from groundscribe.stages.base import PipelineContext
from groundscribe.stages.diffing import structured_diff
from groundscribe.stages.errors import OverrideRejected
from groundscribe.stages.schemas import ArchitectureProposal, ProposedArticle, RiskLevel
from groundscribe.workflow.engine import Override
from groundscribe.workflow.errors import AttributionRequired
from groundscribe.workflow.states import WorkflowAction, WorkflowState

#: The stage name an override is recorded under.
OVERRIDE_STAGE = "override_architecture"

#: How many supporting claims an article needs before thin content stops being
#: the first thing to worry about. Two is not a research finding — it is the
#: point at which an article has something to compare, which is what makes it an
#: argument rather than an assertion.
THIN_CLAIM_THRESHOLD = 2


class OverrideOperation(StrEnum):
    """The seven edits an author may make to a proposed architecture.

    Exactly the seven plan/06 names. They are a closed set because each one is
    recorded in a decision record, and an eighth invented at a call site would be
    an edit nobody could review by name.
    """

    MERGE = "merge"
    SPLIT = "split"
    REMOVE = "remove"
    REORDER = "reorder"
    RENAME = "rename"
    EDIT_THESIS = "edit_thesis"
    REASSIGN_EVIDENCE = "reassign_evidence"


class OverrideCommand(BaseModel):
    """One edit, with whatever that edit needs.

    One shape for all seven rather than seven classes: the command is serialised
    into a decision record, and a union of seven payloads would make that record
    unreadable without knowing which variant to expect.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: OverrideOperation
    article_ids: tuple[str, ...] = ()
    title: str = ""
    titles: tuple[str, ...] = ()
    thesis: str = ""
    claim_ids: tuple[str, ...] = ()
    new_ids: tuple[str, ...] = ()
    order: tuple[str, ...] = ()


class OverrideWarning(BaseModel):
    """A trade-off the author should see, and may ignore."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    article_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArchitectureOverride:
    """Everything one committed override produced."""

    proposal: ArchitectureProposal
    architecture: domain_models.ContentArchitecture
    before_snapshot: ArtifactSnapshot
    after_snapshot: ArtifactSnapshot
    diff_snapshot: ArtifactSnapshot
    warnings: tuple[OverrideWarning, ...]
    decision: models.DecisionRecord
    execution: models.StageExecution


def apply_overrides(
    proposal: ArchitectureProposal, commands: Sequence[OverrideCommand]
) -> tuple[ArchitectureProposal, tuple[OverrideWarning, ...]]:
    """Apply ``commands`` in order, returning the new proposal and its warnings.

    Pure: nothing is persisted and no transition is taken, so a caller can show
    the author what an edit would do before they commit to it. Commands compose —
    each sees the result of the last — because that is how an author works, and a
    batch validated only against the original would reject its own second step.
    """
    articles = list(proposal.articles)
    warnings: list[OverrideWarning] = []

    for command in commands:
        articles, produced = _apply(articles, command)
        warnings.extend(produced)

    if not articles:
        raise OverrideRejected(
            "an architecture needs at least one article; removing them all is a "
            "cancellation, not an override"
        )

    return _resettle(proposal, articles, warnings)


def _apply(
    articles: list[ProposedArticle], command: OverrideCommand
) -> tuple[list[ProposedArticle], list[OverrideWarning]]:
    """Apply one command to the working list of articles."""
    known = {article.id for article in articles}
    referenced = {*command.article_ids, *command.order}
    unknown = sorted(referenced - known)
    if unknown:
        raise OverrideRejected(
            f"override {command.operation.value} names {', '.join(unknown)}, which "
            f"{'is' if len(unknown) == 1 else 'are'} not in the architecture; an override "
            "that edits nothing is a mistake, not a no-op"
        )

    return _HANDLERS[command.operation](articles, command)


def _merge(
    articles: list[ProposedArticle], command: OverrideCommand
) -> tuple[list[ProposedArticle], list[OverrideWarning]]:
    """Fold several articles into the first named one."""
    targets = [article for article in articles if article.id in command.article_ids]
    if len(targets) < 2:
        raise OverrideRejected("a merge needs at least two articles to merge")

    head, *rest = targets
    claims = list(head.supporting_claim_ids)
    for article in rest:
        claims.extend(
            claim_id for claim_id in article.supporting_claim_ids if claim_id not in claims
        )
    merged = head.model_copy(
        update={
            "title": command.title or head.title,
            "thesis": command.thesis or head.thesis,
            "supporting_claim_ids": tuple(claims),
            "overlaps_with": (),
            "thin_content_risk": RiskLevel.LOW,
        }
    )
    remaining = [
        merged if article.id == head.id else article
        for article in articles
        if article.id == head.id or article.id not in command.article_ids
    ]
    return (
        remaining,
        [
            OverrideWarning(
                code="merged_theses",
                message=(
                    f"{len(targets)} articles were merged into {head.id}; the result argues "
                    "more than one thesis unless you also edit it down"
                ),
                article_ids=tuple(command.article_ids),
            )
        ],
    )


def _split(
    articles: list[ProposedArticle], command: OverrideCommand
) -> tuple[list[ProposedArticle], list[OverrideWarning]]:
    """Divide one article into two, sharing its claims between them."""
    if len(command.new_ids) != 2 or len(command.titles) != 2:
        raise OverrideRejected("a split names exactly two new ids and two titles")

    (target,) = [article for article in articles if article.id in command.article_ids]
    first_claims = tuple(command.claim_ids)
    second_claims = tuple(
        claim_id for claim_id in target.supporting_claim_ids if claim_id not in first_claims
    )
    if not first_claims or not second_claims:
        raise OverrideRejected(
            "a split must leave both articles with claims; one of them would argue nothing, "
            "which loses the material rather than dividing it"
        )

    halves = [
        target.model_copy(
            update={
                "id": new_id,
                "title": title,
                "supporting_claim_ids": claims,
                "thin_content_risk": _thin_risk(claims),
                "overlaps_with": tuple(other for other in command.new_ids if other != new_id),
            }
        )
        for new_id, title, claims in zip(
            command.new_ids, command.titles, (first_claims, second_claims), strict=True
        )
    ]
    index = articles.index(target)
    remaining = [*articles[:index], *halves, *articles[index + 1 :]]

    warnings = []
    if any(half.thin_content_risk is RiskLevel.HIGH for half in halves):
        warnings.append(
            OverrideWarning(
                code="thin_after_split",
                message=(
                    f"splitting {target.id} leaves an article with fewer than "
                    f"{THIN_CLAIM_THRESHOLD} supporting claims"
                ),
                article_ids=tuple(command.new_ids),
            )
        )
    return remaining, warnings


def _remove(
    articles: list[ProposedArticle], command: OverrideCommand
) -> tuple[list[ProposedArticle], list[OverrideWarning]]:
    """Drop articles, warning about the claims nothing argues any more."""
    removed = [article for article in articles if article.id in command.article_ids]
    remaining = [article for article in articles if article.id not in command.article_ids]
    kept_claims = {claim_id for article in remaining for claim_id in article.supporting_claim_ids}
    orphaned = sorted(
        {
            claim_id
            for article in removed
            for claim_id in article.supporting_claim_ids
            if claim_id not in kept_claims
        }
    )

    warnings = []
    if orphaned:
        warnings.append(
            OverrideWarning(
                code="orphaned_claims",
                message=(
                    f"no remaining article argues {', '.join(orphaned)}; that material will "
                    "not reach any draft"
                ),
                article_ids=tuple(command.article_ids),
            )
        )
    return remaining, warnings


def _reorder(
    articles: list[ProposedArticle], command: OverrideCommand
) -> tuple[list[ProposedArticle], list[OverrideWarning]]:
    """Put the articles in the author's order."""
    if set(command.order) != {article.id for article in articles}:
        raise OverrideRejected("a reorder must list every article exactly once")
    by_id = {article.id: article for article in articles}
    return [by_id[article_id] for article_id in command.order], []


def _rename(
    articles: list[ProposedArticle], command: OverrideCommand
) -> tuple[list[ProposedArticle], list[OverrideWarning]]:
    """Retitle one article."""
    return _edit(articles, command, {"title": command.title}), []


def _edit_thesis(
    articles: list[ProposedArticle], command: OverrideCommand
) -> tuple[list[ProposedArticle], list[OverrideWarning]]:
    """Change what an article asserts, warning that its evidence was not re-checked."""
    edited = _edit(articles, command, {"thesis": command.thesis})
    return (
        edited,
        [
            OverrideWarning(
                code="thesis_evidence_unchecked",
                message=(
                    "the supporting claims were not re-checked against the new thesis; they "
                    "still say what they said"
                ),
                article_ids=tuple(command.article_ids),
            )
        ],
    )


def _reassign_evidence(
    articles: list[ProposedArticle], command: OverrideCommand
) -> tuple[list[ProposedArticle], list[OverrideWarning]]:
    """Point an article at a different set of claims."""
    if not command.claim_ids:
        raise OverrideRejected("reassigning evidence needs at least one claim")
    edited = _edit(
        articles,
        command,
        {
            "supporting_claim_ids": command.claim_ids,
            "thin_content_risk": _thin_risk(command.claim_ids),
        },
    )
    shared = sorted(
        {
            claim_id
            for claim_id in command.claim_ids
            for article in edited
            if article.id not in command.article_ids and claim_id in article.supporting_claim_ids
        }
    )
    warnings = []
    if shared:
        warnings.append(
            OverrideWarning(
                code="shared_evidence",
                message=(
                    f"{', '.join(shared)} now supports more than one article; the drafts will "
                    "overlap unless you narrow one of them"
                ),
                article_ids=tuple(command.article_ids),
            )
        )
    return edited, warnings


def _edit(
    articles: list[ProposedArticle], command: OverrideCommand, update: dict[str, object]
) -> list[ProposedArticle]:
    """Apply a field update to the named articles."""
    if not command.article_ids:
        raise OverrideRejected(f"{command.operation.value} needs an article to edit")
    return [
        article.model_copy(update=update) if article.id in command.article_ids else article
        for article in articles
    ]


def _thin_risk(claim_ids: Collection[str]) -> RiskLevel:
    """How thin an article arguing exactly these claims is likely to be."""
    return RiskLevel.HIGH if len(claim_ids) < THIN_CLAIM_THRESHOLD else RiskLevel.LOW


def _resettle(
    proposal: ArchitectureProposal,
    articles: list[ProposedArticle],
    warnings: list[OverrideWarning],
) -> tuple[ArchitectureProposal, tuple[OverrideWarning, ...]]:
    """Repair the whole-proposal facts the edits invalidated.

    An override edits articles; the series and the decision are stated *about*
    those articles, and an edit can leave them naming something that no longer
    exists. Repairing them here rather than rejecting the override is the same
    judgement as everywhere else in this module: the author is allowed to make
    this change, and the system's job is to keep the record coherent and say what
    it had to adjust.
    """
    ids = [article.id for article in articles]
    is_series = proposal.series.is_series and len(ids) > 1
    # The reading order follows the articles' own order once they have been
    # edited: an author who reorders them has said what the order is, and a
    # separately stored sequence would be a second opinion on the same question.
    updated_series = proposal.series.model_copy(
        update={"is_series": is_series, "reading_order": tuple(ids) if is_series else ()}
    )

    decision = proposal.decision
    if decision.selected not in ids:
        warnings.append(
            OverrideWarning(
                code="selection_changed",
                message=(
                    f"the recommended article {decision.selected!r} is gone; {ids[0]!r} is now "
                    "the lead"
                ),
                article_ids=(ids[0],),
            )
        )
        decision = decision.model_copy(update={"selected": ids[0]})

    return (
        proposal.model_copy(
            update={
                "articles": tuple(articles),
                "series": updated_series,
                "decision": decision,
            }
        ),
        tuple(warnings),
    )


def approve_architecture(
    context: PipelineContext,
    *,
    architecture: domain_models.ContentArchitecture,
    snapshot: ArtifactSnapshot,
    approved_by: str,
) -> models.DecisionRecord:
    """Approve an architecture and lock it (phase 06 §5).

    The lock is a row-level fact so "was this approved when it changed?" is
    answerable without replaying the run; the engine's guard is what enforces it
    on the way through. Passing the snapshot to the transition is what tells the
    engine *which* architecture is now the approved one.
    """
    if not approved_by:
        raise AttributionRequired("approved_by is required: an anonymous approval is unreviewable")

    execution = context.engine.begin_stage("approve_architecture", impl_version="1.0")
    context.recorder.record_user_intervention(
        execution,
        user_id=approved_by,
        intervention_type=InterventionType.APPROVAL,
        payload={"architecture_id": architecture.id, "snapshot_id": snapshot.id},
    )
    recorded = context.engine.apply(
        WorkflowAction.APPROVE_ARCHITECTURE,
        actor_id=approved_by,
        actor_type=ActorType.USER,
        artifacts=(snapshot,),
        rationale="the author approved the proposed architecture",
    )
    architecture.locked = True
    architecture.locked_by = approved_by
    context.session.flush()
    context.recorder.complete_stage(execution)
    return recorded.decision


def override_architecture(
    context: PipelineContext,
    *,
    architecture: domain_models.ContentArchitecture,
    proposal: ArchitectureProposal,
    snapshot: ArtifactSnapshot,
    commands: Sequence[OverrideCommand],
    requested_by: str,
    reason: str = "",
    accepted_warnings: Collection[str] = (),
) -> ArchitectureOverride:
    """Commit an author's edits as a new, unlocked version of the architecture.

    The sequence is deliberate: reopen (a person's decision, a user edge), write
    the new version as a fork of the approved one, then re-submit it for review
    carrying the override. The engine's guard sees a snapshot whose parent is the
    approved one *and* an override naming who asked for it, which is what plan/05
    requires before an approved architecture may be superseded.
    """
    if not requested_by:
        raise AttributionRequired("requested_by is required: an anonymous override is unreviewable")

    edited, warnings = apply_overrides(proposal, commands)
    execution = context.engine.begin_stage(OVERRIDE_STAGE, impl_version="1.0")
    context.recorder.record_input(execution, snapshot, role="content_architecture")

    reopen = (
        WorkflowAction.REOPEN_ARCHITECTURE
        if context.engine.state is WorkflowState.ARCHITECTURE_APPROVED
        else WorkflowAction.REJECT_ARCHITECTURE
    )
    context.engine.apply(
        reopen,
        actor_id=requested_by,
        actor_type=ActorType.USER,
        rationale=reason or "the author edited the architecture",
    )

    after = context.recorder.record_output(
        execution,
        artifact_type=ArtifactType.CONTENT_ARCHITECTURE,
        content=edited.model_dump(mode="json"),
        role="content_architecture",
        parent=snapshot,
    )
    diff = context.recorder.record_output(
        execution,
        artifact_type=ArtifactType.STRUCTURED_DIFF,
        content=structured_diff(
            proposal.model_dump(mode="json"), edited.model_dump(mode="json")
        ).model_dump(mode="json"),
        role="architecture_diff",
    )

    architecture.branch_status = BranchStatus.SUPERSEDED
    branched = domain_models.ContentArchitecture(
        id=uuid.uuid4().hex,
        project_id=context.project_id,
        summary=edited.decision.rationale or edited.articles[0].thesis,
        snapshot_id=after.id,
        created_by_execution_id=execution.id,
        parent_id=architecture.id,
    )
    context.session.add(branched)
    context.session.flush()

    accepted = [warning.code for warning in warnings if warning.code in set(accepted_warnings)]
    context.recorder.record_user_intervention(
        execution,
        user_id=requested_by,
        intervention_type=InterventionType.OVERRIDE,
        payload={
            "operations": [command.operation.value for command in commands],
            "reason": reason,
            "warnings_accepted": accepted,
        },
    )
    decision = context.recorder.record_decision(
        execution,
        decision_type="architecture_override",
        decided_by=requested_by,
        decided_by_type=ActorType.USER,
        inputs={
            "operations": [command.operation.value for command in commands],
            "commands": [command.model_dump(mode="json") for command in commands],
            "warnings_shown": [warning.model_dump(mode="json") for warning in warnings],
            "warnings_accepted": accepted,
            "before_snapshot_id": snapshot.id,
            "after_snapshot_id": after.id,
            "diff_snapshot_id": diff.id,
        },
        outcome=branched.id,
        rationale=reason,
    )
    context.recorder.complete_stage(execution)

    # Back to review, carrying the override the engine's guard demands.
    context.engine.apply(
        WorkflowAction.SUBMIT_ARCHITECTURE,
        artifacts=(after,),
        override=Override(requested_by=requested_by, reason=reason),
        rationale="the overridden architecture returns for review",
    )
    return ArchitectureOverride(
        proposal=edited,
        architecture=branched,
        before_snapshot=snapshot,
        after_snapshot=after,
        diff_snapshot=diff,
        warnings=warnings,
        decision=decision,
        execution=execution,
    )


#: One handler per operation, resolved by lookup rather than by a chain of
#: branches: the seven are a closed set, and a table makes an unhandled member a
#: KeyError at the call site instead of a silent no-op at the bottom of an
#: if/elif. Declared after the handlers so they are defined when it is built.
_HANDLERS: dict[
    OverrideOperation,
    Callable[
        [list[ProposedArticle], OverrideCommand],
        tuple[list[ProposedArticle], list[OverrideWarning]],
    ],
] = {
    OverrideOperation.MERGE: _merge,
    OverrideOperation.SPLIT: _split,
    OverrideOperation.REMOVE: _remove,
    OverrideOperation.REORDER: _reorder,
    OverrideOperation.RENAME: _rename,
    OverrideOperation.EDIT_THESIS: _edit_thesis,
    OverrideOperation.REASSIGN_EVIDENCE: _reassign_evidence,
}


__all__ = [
    "OVERRIDE_STAGE",
    "THIN_CLAIM_THRESHOLD",
    "ArchitectureOverride",
    "OverrideCommand",
    "OverrideOperation",
    "OverrideWarning",
    "apply_overrides",
    "approve_architecture",
    "override_architecture",
]
