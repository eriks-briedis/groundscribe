"""Source-of-truth extraction (phase 06 §2).

plan/06 → *ExtractSourceTruth*: the structured source model, recorded with its
schema version, rendered prompt, the segments included and excluded (with
reasons), the token budget and any truncation, the raw and parsed responses,
validation failures, repairs, the final accepted model, and the configuration it
ran under.

Most of that list is already guaranteed by phase 04's generator — it renders the
versioned template, records the effective request, chains repair attempts, and
stores raw/parsed/validated responses separately. What this stage adds is the two
things the generator cannot know:

**What the model was allowed to see.** Segments are selected against a token
budget and the selection is recorded candidate by candidate, including the ones
that were excluded or truncated. plan/03's reasoning applies directly: what the
model could *not* see explains as many surprising outputs as what it could.

**Whether the answer points at anything real.** A response citing a segment id
that was never sent is schema-valid and false, so the repair ladder cannot see it.
The stage checks the citations against the document it actually sent, and fails
rather than storing a source model whose evidence links nowhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import AnswerResponse, ArtifactType
from groundscribe.domain.models import ArtifactSnapshot, SourceSegment
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.provenance.enums import ContextDisposition
from groundscribe.provenance.schemas import ContextCandidate
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.diffing import structured_diff
from groundscribe.stages.errors import EvidenceError, ProviderNotPermitted
from groundscribe.stages.ingestion import IngestedSource
from groundscribe.stages.schemas import SourceModel
from groundscribe.workflow.states import WorkflowAction

#: The stage name, which is also its routing key and its prompt template id.
#: One name for all three on purpose: a stage whose prompt, route and execution
#: record could drift apart would be untraceable in exactly the case that matters.
EXTRACTION_STAGE = "extract_source_truth"

#: The versioned context-selection strategy recorded with every run of it.
EXTRACTION_STRATEGY = "source_segments_in_order"
EXTRACTION_STRATEGY_VERSION = "1"

#: Default budget for the source material in one extraction call.
DEFAULT_TOKEN_BUDGET = 6000

#: Characters per token. A heuristic, and deliberately a crude one: it is used to
#: decide what fits, and it is *recorded* alongside the decision, so a later phase
#: swapping in a real tokeniser changes the numbers without changing the meaning.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class SelectedSegment:
    """One segment as offered to the model, possibly shortened to fit."""

    segment: SourceSegment
    text: str
    truncated: bool

    @property
    def id(self) -> str:
        return self.segment.id

    @property
    def kind(self) -> str:
        return self.segment.kind.value


@dataclass(frozen=True)
class ContextWindow:
    """What was chosen for one call, and what became of everything else."""

    selected: tuple[SelectedSegment, ...]
    candidates: tuple[ContextCandidate, ...]
    token_budget: int


def select_segments(segments: Sequence[SourceSegment], *, token_budget: int) -> ContextWindow:
    """Fit the source into ``token_budget``, recording the fate of every segment.

    Segments are taken in document order rather than by relevance. Extraction
    reads the *whole* source; there is no query to be relevant to, and reordering
    it would break the development history the order encodes. Retrieval-based
    selection arrives in phase 12, which is why the strategy is versioned now.
    """
    selected: list[SelectedSegment] = []
    candidates: list[ContextCandidate] = []
    remaining = token_budget * CHARS_PER_TOKEN

    for segment in segments:
        length = len(segment.text)
        if remaining <= 0:
            candidates.append(
                ContextCandidate(
                    reference=segment.id,
                    disposition=ContextDisposition.EXCLUDED,
                    reason=f"beyond the {token_budget}-token context budget",
                )
            )
            continue
        if length <= remaining:
            selected.append(SelectedSegment(segment=segment, text=segment.text, truncated=False))
            candidates.append(
                ContextCandidate(
                    reference=segment.id,
                    disposition=ContextDisposition.SELECTED,
                    reason=f"fits the {token_budget}-token context budget",
                )
            )
            remaining -= length
            continue
        selected.append(
            SelectedSegment(segment=segment, text=segment.text[:remaining], truncated=True)
        )
        candidates.append(
            ContextCandidate(
                reference=segment.id,
                disposition=ContextDisposition.TRUNCATED,
                reason=(
                    f"cut to {remaining} of {length} characters by the "
                    f"{token_budget}-token context budget"
                ),
            )
        )
        remaining = 0

    return ContextWindow(
        selected=tuple(selected), candidates=tuple(candidates), token_budget=token_budget
    )


class ExtractSourceTruth:
    """Turn ingested source material into the structured source model.

    Declares no exit edge. Extraction leaves the run in ``SOURCE_MODEL_EXTRACTING``
    because whether the source model is *ready* is not extraction's call — gap
    analysis (phase 06 §3) decides whether the run completes or parks for the
    author, and it decides that from the gaps it finds.
    """

    name: ClassVar[str] = EXTRACTION_STAGE
    impl_version: ClassVar[str] = "1.1"
    exit_action: ClassVar[WorkflowAction | None] = None

    #: An instance attribute, not a class one: extraction is entered from
    #: ``SOURCE_INGESTED`` the first time and re-entered from the answer pause,
    #: where the person answering has already taken the edge (phase 06 §3).
    entry_action: WorkflowAction | None

    def __init__(
        self,
        *,
        source: IngestedSource,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        template_version: str | None = None,
        override: RouteOverride | None = None,
        entry_action: WorkflowAction | None = WorkflowAction.EXTRACT_SOURCE_MODEL,
        answers: Sequence[domain_models.UserAnswer] = (),
        previous: SourceModel | None = None,
        previous_snapshot: ArtifactSnapshot | None = None,
    ) -> None:
        self._source = source
        self._token_budget = token_budget
        self._template_version = template_version
        self._override = override
        self.entry_action = entry_action
        self._answers = tuple(answers)
        self._previous = previous
        self._previous_snapshot = previous_snapshot

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[SourceModel]:
        """Select context, generate, check the citations, store the model."""
        require_permitted_provider(context, EXTRACTION_STAGE, override=self._override)

        window = select_segments(self._source.segments, token_budget=self._token_budget)
        context.recorder.record_context_selection(
            execution,
            strategy=EXTRACTION_STRATEGY,
            strategy_version=EXTRACTION_STRATEGY_VERSION,
            candidates=window.candidates,
            token_budget=window.token_budget,
        )
        context.recorder.record_input(execution, self._source.snapshot, role="source_document")

        generated = await context.generator.generate(
            execution,
            stage=EXTRACTION_STAGE,
            template_id=EXTRACTION_STAGE,
            template_version=self._template_version,
            variables={
                "segments": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "text": item.text,
                        "truncated": item.truncated,
                    }
                    for item in window.selected
                ],
                "audience": context.constraints.audience,
                "platform": context.constraints.platform,
                "depth": context.constraints.depth.value,
                "answers": [_render_answer(answer) for answer in self._sendable_answers()],
            },
            schema=SourceModel,
            override=self._override,
        )
        model = generated.value
        check_citations(model.cited_segment_ids(), window)

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.SOURCE_MODEL,
            content=model.model_dump(mode="json"),
            role="source_model",
            # A rebuild forks from the model it replaces, so the earlier version
            # stays readable and comparable (plan/00 → no silent mutation).
            parent=self._previous_snapshot,
        )
        outputs = (snapshot, *self._record_diff(context, execution, model))
        return StageResult(
            value=model,
            outputs=outputs,
            invocations=generated.attempts,
            usage=generated.usage,
            detail={
                "claims": len(model.claims),
                "segments_offered": len(window.selected),
                "token_budget": window.token_budget,
                "answers_applied": len(self._sendable_answers()),
            },
        )

    def _sendable_answers(self) -> tuple[domain_models.UserAnswer, ...]:
        """The answers whose text may go into the prompt.

        Filtered here as well as in the queue: this is the point where the text
        would actually cross the wire, and a guard that only lives at the caller
        is a guard the next caller will forget.
        """
        return tuple(answer for answer in self._answers if answer.response_type.may_be_sent)

    def _record_diff(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        model: SourceModel,
    ) -> tuple[ArtifactSnapshot, ...]:
        """Store what this rebuild changed, and link it to the answers that caused it.

        The diff is an artefact rather than a computed view because it is what a
        person reviews and what an answer record points at. Linking it back to the
        answers completes their provenance: the answer, and what it did.
        """
        if self._previous is None:
            return ()
        diff = structured_diff(
            self._previous.model_dump(mode="json"), model.model_dump(mode="json")
        )
        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.STRUCTURED_DIFF,
            content=diff.model_dump(mode="json"),
            role="source_model_diff",
        )
        for answer in self._answers:
            answer.diff_snapshot_id = snapshot.id
        context.session.flush()
        return (snapshot,)


def _render_answer(answer: domain_models.UserAnswer) -> dict[str, object]:
    """One answer in the shape the prompt renders it.

    A corrected premise is flagged rather than presented as an answer: the model
    has to *stop assuming* the premise, which is a different instruction from
    recording a new fact.
    """
    return {
        "question": answer.question,
        "text": answer.text,
        "premise_incorrect": answer.response_type is AnswerResponse.PREMISE_INCORRECT,
    }


def require_permitted_provider(
    context: PipelineContext, stage: str, *, override: RouteOverride | None = None
) -> None:
    """Refuse to send this project's material to a provider it has not allowed.

    Resolved through the same routing policy the generator will use, so the
    provider checked is the provider called — asking the policy twice is cheaper
    than the class of bug where the check and the call disagree.
    """
    route = context.generator.routing.resolve(stage, override=override)
    provider = route.primary.provider
    if not context.constraints.permits_provider(provider):
        allowed = ", ".join(context.constraints.allowed_providers) or "none"
        raise ProviderNotPermitted(
            f"stage {stage!r} routes to provider {provider!r}, which this project has not "
            f"allowed (allowed: {allowed}); no material has been sent"
        )


def check_citations(cited: frozenset[str], window: ContextWindow) -> None:
    """Fail unless every cited segment is one that was actually shown.

    Compared against what was *sent*, not against the whole document: a citation
    of a segment the budget excluded is as unverifiable as one of a segment that
    never existed, and treating them differently would let a truncated run quietly
    produce evidence the model could not have read.
    """
    offered = {item.id for item in window.selected}
    dangling = sorted(cited - offered)
    if dangling:
        raise EvidenceError(
            f"the source model cites {', '.join(dangling)}, which "
            f"{'was' if len(dangling) == 1 else 'were'} not among the "
            f"{len(offered)} segments sent to the model; a citation nobody can check is "
            "worse than none, because it looks checked"
        )


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_TOKEN_BUDGET",
    "EXTRACTION_STAGE",
    "EXTRACTION_STRATEGY",
    "EXTRACTION_STRATEGY_VERSION",
    "ContextWindow",
    "ExtractSourceTruth",
    "SelectedSegment",
    "check_citations",
    "require_permitted_provider",
    "select_segments",
]
