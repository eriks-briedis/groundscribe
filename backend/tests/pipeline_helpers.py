"""Walking a run through phase 06 so a phase-07 test can start where it needs to.

Drafting begins where the brief ends, and the brief is five stages downstream of
an ingested document. Rather than assign the machine a state and hand-build the
artefacts — which would test the phase-07 stages against inputs no run ever
produces — these helpers drive the real phase-06 stages with scripted answers and
hand back what they made.

That costs a few hundred milliseconds per test and buys the thing that matters: a
draft written against a brief that was actually generated from an architecture
that was actually approved, with the provenance chain intact behind it.
"""

from __future__ import annotations

from dataclasses import dataclass

from golden import golden_json, with_segment_ids
from groundscribe.domain import models as domain_models
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.llm import FakeLLMClient
from groundscribe.provenance.enums import ActorType
from groundscribe.stages.architecture import ARCHITECTURE_STAGE, ProposeContentArchitecture
from groundscribe.stages.base import PipelineContext, StageRunner
from groundscribe.stages.brief import BRIEF_STAGE, GenerateArticleBrief
from groundscribe.stages.extraction import ExtractSourceTruth
from groundscribe.stages.ingestion import IngestedSource
from groundscribe.stages.override import approve_architecture
from groundscribe.stages.schemas import ArticleBriefDocument, ProposedArticle, SourceModel
from groundscribe.workflow.states import WorkflowAction
from stage_helpers import DEFAULT_CONSTRAINTS
from test_extraction import ingest_golden, script

#: The seeded project's author, and the person every human action is attributed to.
AUTHOR = "u1"


@dataclass(frozen=True)
class BriefedArticle:
    """Everything phase 06 produced that a phase-07 stage needs."""

    source: IngestedSource
    source_model: SourceModel
    source_model_snapshot: ArtifactSnapshot
    article: ProposedArticle
    concept: domain_models.ArticleConcept
    brief: ArticleBriefDocument
    brief_snapshot: ArtifactSnapshot
    brief_row: domain_models.ArticleBrief


async def run_to_approved_brief(
    context: PipelineContext, model_client: FakeLLMClient
) -> BriefedArticle:
    """Drive the phase-06 chain to an approved brief, leaving the run at drafting."""
    source = await ingest_golden(context)
    script(model_client, with_segment_ids(golden_json("source_model.json"), source))
    extracted = await StageRunner(context).run(ExtractSourceTruth(source=source))
    context.engine.apply(WorkflowAction.COMPLETE_EXTRACTION)

    model_client.script_response(ARCHITECTURE_STAGE, golden_json("architecture.json"))
    proposed = await StageRunner(context).run(
        ProposeContentArchitecture(
            source_model=extracted.value, source_model_snapshot=extracted.outputs[0]
        )
    )
    approve_architecture(
        context,
        architecture=proposed.value.architecture,
        snapshot=proposed.outputs[0],
        approved_by=AUTHOR,
    )

    concept = proposed.value.concept(proposed.value.proposal.decision.selected)
    assert concept is not None
    article = proposed.value.proposal.article(concept.id)
    assert article is not None

    model_client.script_response(BRIEF_STAGE, golden_json("brief.json"))
    briefed = await StageRunner(context).run(
        GenerateArticleBrief(
            concept=concept,
            article=article,
            source_model=extracted.value,
            architecture_snapshot=proposed.outputs[0],
        )
    )
    # The author approves the brief, which is what opens drafting.
    context.engine.apply(
        WorkflowAction.APPROVE_BRIEF,
        actor_id=AUTHOR,
        actor_type=ActorType.USER,
        artifacts=(briefed.outputs[0],),
    )

    return BriefedArticle(
        source=source,
        source_model=extracted.value,
        source_model_snapshot=extracted.outputs[0],
        article=article,
        concept=concept,
        brief=briefed.value.brief,
        brief_snapshot=briefed.outputs[0],
        brief_row=briefed.value.row,
    )


__all__ = ["AUTHOR", "BriefedArticle", "run_to_approved_brief"]
