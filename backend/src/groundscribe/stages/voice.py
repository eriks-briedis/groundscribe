"""Line editing and voice alignment (phase 07 §11).

plan/07 → *AlignVoice*: a style-only pass. Permitted — rhythm, word choice, flow,
repetition, formality, mechanical transitions, unnatural phrasing, excessive
abstraction, generic AI patterns. Prohibited — new claims, new examples, new
technical detail, changed evidence, a changed thesis, removed qualifications,
significant structural change. On discovering a structural problem, route back to
substantive revision rather than silently changing it.

**The prohibitions are structural, not enforced.** A voice pass returns a body and
a list of declared edits, and nothing else. The next version is the previous one
with its prose replaced — the thesis, the claims, the qualifications and the
omissions are copied across untouched, because there is no field through which the
pass could have changed them. A guard that *rejected* prohibited changes would
still let a model attempt one; this way there is nothing to attempt.

What remains for the stage to check is whether the pass reported itself honestly:

- each declared edit's ``before`` was in the previous prose, and its ``after`` is in
  the new prose. A pass claiming to have rephrased something absent is inventing
  its own record, which is worse than a bad edit because the record is what anyone
  reviewing it will read;
- every unresolved marker survives. Deleting one publishes a hole as though it were
  an answer, and it is precisely the tidy-up a style pass would think it was doing
  a favour by making.

A structural problem stops the *transition*, not the stage. The safe edits are made
and stored, the problem is recorded and a person is asked for — and the article does
not go on to scoring carrying a known structural fault. The phase-05 table has no
edge from voice alignment back to revision, and inventing one belongs to phase 05.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.drafting import DraftOutcome, check_excluded_material, store_version
from groundscribe.stages.errors import VoiceContractError, VoiceRuleViolation
from groundscribe.stages.extraction import require_permitted_provider
from groundscribe.stages.schemas import (
    ArticleBriefDocument,
    ArticleDraft,
    VoicePass,
    VoiceProfileDocument,
)
from groundscribe.voice.schemas import VoiceInstruction
from groundscribe.workflow.states import WorkflowAction

#: The stage name, routing key and prompt template id.
VOICE_STAGE = "align_voice"


class AlignVoice:
    """Read the article as prose, and change only how it reads."""

    name: ClassVar[str] = VOICE_STAGE
    impl_version: ClassVar[str] = "1.0"
    #: No declared edges. The entry was taken by whatever accepted the review, and
    #: the exit depends on whether the pass found something it must not fix.
    entry_action: ClassVar[WorkflowAction | None] = None
    exit_action: ClassVar[WorkflowAction | None] = None

    def __init__(
        self,
        *,
        previous: ArticleDraft,
        parent: domain_models.ArticleVersion,
        concept: domain_models.ArticleConcept,
        brief: ArticleBriefDocument,
        voice: VoiceProfileDocument,
        template_version: str | None = None,
        override: RouteOverride | None = None,
    ) -> None:
        self._previous = previous
        self._parent = parent
        self._concept = concept
        self._brief = brief
        self._voice = voice
        self._template_version = template_version
        self._override = override

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[DraftOutcome]:
        """Align the prose, check the pass reported itself honestly, store the version."""
        require_permitted_provider(context, VOICE_STAGE, override=self._override)
        if self._parent.snapshot is not None:
            context.recorder.record_input(execution, self._parent.snapshot, role="article_version")

        generated = await context.generator.generate(
            execution,
            stage=VOICE_STAGE,
            template_id=VOICE_STAGE,
            template_version=self._template_version,
            variables={
                "body": self._previous.body,
                "voice": self._voice.model_dump(mode="json"),
                "thesis": self._previous.thesis,
                "markers": [item.marker for item in self._previous.unresolved],
                "first_person_allowed": context.constraints.first_person_allowed,
            },
            schema=VoicePass,
            override=self._override,
        )
        passed = generated.value
        check_voice_pass(passed, self._previous, self._brief)
        _enforce_hard_rules(passed.body, self._voice)

        # The new version is the old one with its prose replaced. Nothing else can
        # change, because nothing else came back from the model.
        aligned = self._previous.model_copy(update={"body": passed.body})

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.ARTICLE_VERSION,
            content=aligned.model_dump(mode="json"),
            role="article_version",
            parent=self._parent.snapshot,
        )
        article, version = store_version(
            context, execution, aligned, snapshot, concept=self._concept, parent=self._parent
        )
        blocked = self._report_structural(context, execution, passed)

        return StageResult(
            value=DraftOutcome(draft=aligned, article=article, version=version),
            outputs=(snapshot,),
            invocations=generated.attempts,
            usage=generated.usage,
            # A known structural fault does not travel on to scoring. The pass
            # applied what it safely could and hands the run to a person — which
            # for a long time meant handing it nowhere: taking no edge left the
            # run in `voice_aligning`, whose only other exit is the one just
            # declined, and auto-advance then ran the same pass again on every
            # completion.
            exit_action=(
                WorkflowAction.VOICE_BLOCKED if blocked else WorkflowAction.SUBMIT_VOICE_PASS
            ),
            detail={
                "changes": len(passed.changes),
                "structural_problems": len(passed.structural_problems),
                "voice_profile": self._voice.name,
                "voice_profile_version": self._voice.version,
            },
        )

    def _report_structural(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        passed: VoicePass,
    ) -> bool:
        """Record a structural problem the pass refused to fix; True if it found one."""
        if not passed.structural_problems:
            return False

        context.recorder.record_decision(
            execution,
            decision_type="voice_structural_return",
            decided_by=VOICE_STAGE,
            decided_by_type=ActorType.POLICY,
            policy_version=self.impl_version,
            inputs={
                "problems": [
                    problem.model_dump(mode="json") for problem in passed.structural_problems
                ],
                "routes": [problem.suggested_route.value for problem in passed.structural_problems],
            },
            outcome="substantive_revision_requested",
            rationale=(
                "a style pass found a problem no rephrasing fixes; changing it here would be "
                "a structural edit made under a stylistic mandate"
            ),
        )
        context.recorder.emit(
            event_type="intervention.requested",
            actor_type=ActorType.SYSTEM,
            actor_id=VOICE_STAGE,
            execution=execution,
            payload={
                "reason": "voice alignment found a structural problem",
                "problems": [problem.description for problem in passed.structural_problems],
            },
        )
        return True


@dataclass(frozen=True)
class RuleViolation:
    """One hard rule the finished prose broke, and the term that broke it."""

    instruction: VoiceInstruction
    found: str


def check_hard_rules(body: str, voice: VoiceProfileDocument) -> tuple[RuleViolation, ...]:
    """Every hard rule ``body`` breaks (phase 10).

    Hard rules only. plan/10 says a strong preference *allows justified
    exceptions*, so checking preferences here would delete the distinction
    between the two strengths — whatever a profile said, the system would enforce
    both, and the strength model would be decoration.

    Every violation is reported rather than the first: a person fixing one at a
    time is a person running the pass four times to learn four things.
    """
    return tuple(
        RuleViolation(instruction=rule, found=term)
        for rule in voice.hard_rules
        for term in rule.prohibits
        if term in body
    )


def check_voice_pass(
    passed: VoicePass, previous: ArticleDraft, brief: ArticleBriefDocument
) -> None:
    """Refuse a pass that misreports its own edits, drops a marker, or reopens the brief.

    The prose is the only thing worth re-checking. Every other declaration — the
    claims, the qualifications, the omissions — is copied from the previous version
    rather than returned by the model, so re-running the draft checks over them
    would compare each declaration against itself.
    """
    for change in passed.changes:
        if change.before and change.before not in previous.body:
            raise VoiceContractError(
                f"the pass says it changed {change.before!r}, which the previous version "
                "never contained; an edit that cannot be located is a record nobody can check"
            )
        if change.after and change.after not in passed.body:
            raise VoiceContractError(
                f"the pass says it wrote {change.after!r}, which it did not actually write; "
                "the declared result has to be the actual result"
            )

    lost = [item.marker for item in previous.unresolved if item.marker not in passed.body]
    if lost:
        raise VoiceContractError(
            f"the pass removed the unresolved marker(s) {', '.join(lost)}; deleting a marker "
            "publishes a hole as though it were an answer"
        )

    check_excluded_material(passed.body, brief)


def _enforce_hard_rules(body: str, voice: VoiceProfileDocument) -> None:
    """Stop the article if the prose breaks a rule the author called hard.

    Raising, rather than correcting or warning. The stage cannot rewrite the
    sentence — rephrasing is the model's job and has just been done — and a
    warning attached to a stored version is a warning that travels to scoring
    with the violation still in it. What is left is to stop, keep the trace, and
    let a person or a rerun decide.
    """
    broken = check_hard_rules(body, voice)
    if not broken:
        return
    detail = "; ".join(f"{violation.instruction.id} ({violation.found!r})" for violation in broken)
    raise VoiceRuleViolation(
        f"the voice pass wrote prose breaking {len(broken)} hard rule(s) of "
        f"{voice.name}@{voice.version}: {detail}"
    )


__all__ = [
    "VOICE_STAGE",
    "AlignVoice",
    "RuleViolation",
    "check_hard_rules",
    "check_voice_pass",
]
