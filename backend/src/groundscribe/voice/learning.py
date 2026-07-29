"""Inferring a rule from repeated edits, and waiting to be asked (phase 10).

plan/10 → *detect pattern → present inferred preference → show supporting
examples → ask to make it a permanent rule → store only after approval.*

The arrow that matters is the last one. Detection is arithmetic over the author's
own corrections and would be easy to act on directly; acting on it directly is
what this module exists to refuse. A system that turned three edits into a
permanent rule would keep teaching itself things nobody agreed to, each one
invisible until it appeared in prose, and a rule that governs everything the
author will ever publish is exactly what plan/00 means by a high-leverage
decision.

Detection is deliberately literal: the same word removed, three times, in edits
the author marked as being about style. No model is asked what the edits "mean" —
a suggestion the author cannot trace back to their own sentences is one they
cannot argue with, and being able to disagree is the point of asking.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain.enums import FindingStatus
from groundscribe.domain.models import ArticleVersion
from groundscribe.provenance import models as provenance_models
from groundscribe.provenance.enums import InterventionType
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.voice.enums import InstructionStrength, VoiceCategory, VoiceScope
from groundscribe.voice.models import ManualEdit, VoiceSuggestion
from groundscribe.voice.schemas import VoiceInstruction, VoiceProfileDocument

#: How many times a correction has to recur before it is a habit rather than a
#: coincidence. Three, because two is the number of times anyone does anything by
#: accident, and interrupting an author about a word they happened to dislike
#: twice is how a useful feature becomes noise.
HABIT_THRESHOLD = 3

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


@dataclass(frozen=True)
class EditPattern:
    """A correction the author has made more than once.

    ``removed`` is the word that keeps going; ``added`` is what tends to replace
    it, kept because "dramatic → a number" and "dramatic → nothing" are different
    habits and the rationale shown to the author should say which one this is.
    """

    removed: str
    added: str
    occurrences: int
    edit_ids: tuple[str, ...]

    @property
    def instruction_id(self) -> str:
        """The id the rule would carry.

        Derived from the habit rather than generated, so approving the same
        habit again refines one instruction instead of accumulating several that
        say the same thing.
        """
        return f"learned-{self.removed}"


def detect_edit_patterns(
    edits: Sequence[ManualEdit], *, threshold: int = HABIT_THRESHOLD
) -> tuple[EditPattern, ...]:
    """Every recurring correction in ``edits``.

    A word is "removed" when it appears in the text the author replaced and not
    in what they replaced it with. Word-level rather than diff-level because that
    is the granularity a voice instruction is written at — the author's habit is
    about a word, not about a span of characters that happened to change.
    """
    grouped: dict[str, list[ManualEdit]] = defaultdict(list)
    replacements: dict[str, list[str]] = defaultdict(list)

    for edit in edits:
        before = set(_words(edit.before))
        after = set(_words(edit.after))
        for word in sorted(before - after):
            grouped[word].append(edit)
            replacements[word].extend(sorted(after - before))

    return tuple(
        EditPattern(
            removed=word,
            added=_most_common(replacements[word]),
            occurrences=len(matched),
            edit_ids=tuple(edit.id for edit in matched),
        )
        for word, matched in sorted(grouped.items())
        if len(matched) >= threshold
    )


class VoiceLearning:
    """Records edits, offers what they imply, and changes nothing until asked."""

    def __init__(self, session: Session, *, recorder: ProvenanceRecorder) -> None:
        self.session = session
        self._recorder = recorder

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------

    def record_edit(
        self,
        *,
        version: ArticleVersion,
        before: str,
        after: str,
        edited_by: str,
        eligible: bool,
    ) -> ManualEdit:
        """Store one hand edit, and say whether it may teach anything.

        Written as both a row and a phase-03 intervention. The row is evidence a
        pattern is inferred from; the intervention is what makes the article's
        provenance say a person was here — without it, the record would describe
        only what the model did.
        """
        if not edited_by:
            raise ValueError("edited_by is required: an unattributed edit is unattributable")

        run = _run_of(version, self.session)
        execution = self._recorder.start_stage(run, stage="manual_edit", impl_version="1.0")
        edit = ManualEdit(
            id=uuid.uuid4().hex,
            article_version_id=version.id,
            before=before,
            after=after,
            made_by=edited_by,
            voice_training_eligible=eligible,
            made_at=self._recorder.clock(),
            created_by_execution_id=execution.id,
        )
        self.session.add(edit)
        self.session.flush()

        self._recorder.record_user_intervention(
            execution,
            user_id=edited_by,
            intervention_type=InterventionType.EDIT,
            payload={
                "manual_edit_id": edit.id,
                "article_version_id": version.id,
                "voice_training_eligible": eligible,
            },
        )
        self._recorder.complete_stage(execution)
        return edit

    def training_edits(self, user_id: str) -> tuple[ManualEdit, ...]:
        """The edits this author allowed to be used as evidence about their style."""
        return tuple(
            self.session.scalars(
                select(ManualEdit)
                .where(
                    ManualEdit.made_by == user_id,
                    ManualEdit.voice_training_eligible.is_(True),
                )
                .order_by(ManualEdit.made_at, ManualEdit.id)
            )
        )

    def interventions_for(self, edit: ManualEdit) -> tuple[provenance_models.UserIntervention, ...]:
        """The interventions recorded for one edit, so the chain is traversable."""
        return tuple(
            self.session.scalars(
                select(provenance_models.UserIntervention).where(
                    provenance_models.UserIntervention.stage_execution_id
                    == edit.created_by_execution_id
                )
            )
        )

    # ------------------------------------------------------------------
    # Suggestions
    # ------------------------------------------------------------------

    def suggest(
        self,
        pattern: EditPattern,
        *,
        user_id: str,
        category: VoiceCategory,
        scope: VoiceScope = VoiceScope.GLOBAL,
        project_id: str | None = None,
    ) -> VoiceSuggestion:
        """Offer the rule a pattern implies, without applying it.

        Returns the existing suggestion when one has already been raised for this
        habit — including one already refused. Asking again about something the
        author has answered is how a gate becomes nagging, and the record of the
        refusal is what makes not asking possible.
        """
        existing = self.session.scalars(
            select(VoiceSuggestion).where(
                VoiceSuggestion.user_id == user_id, VoiceSuggestion.habit == pattern.instruction_id
            )
        ).first()
        if existing is not None:
            return existing

        suggestion = VoiceSuggestion(
            id=uuid.uuid4().hex,
            user_id=user_id,
            scope=scope,
            project_id=project_id,
            habit=pattern.instruction_id,
            instruction=self._instruction_for(pattern, category).model_dump(mode="json"),
            evidence=self._evidence_for(pattern),
            status=FindingStatus.PROPOSED,
            created_at=self._recorder.clock(),
        )
        self.session.add(suggestion)
        self.session.flush()
        return suggestion

    def approve(
        self,
        suggestion: VoiceSuggestion,
        *,
        profile: VoiceProfileDocument,
        approved_by: str,
        version: str,
    ) -> VoiceProfileDocument:
        """Make an inferred rule permanent, on a person's explicit say-so.

        The only path in this module that changes a profile, and it returns a
        *new* version. Editing the old one would make every article recording
        "written under ada@1" describe a document that no longer exists.
        """
        if not approved_by:
            raise ValueError("approved_by is required: an anonymous approval is unreviewable")
        self._require_undecided(suggestion)

        suggestion.status = FindingStatus.ACCEPTED
        suggestion.decided_by = approved_by
        suggestion.decided_at = self._recorder.clock()
        self.session.flush()

        instruction = VoiceInstruction.model_validate(suggestion.instruction)
        return profile.with_instructions(instruction, version=version)

    def reject(self, suggestion: VoiceSuggestion, *, rejected_by: str, reason: str = "") -> None:
        """Record that the author said no, and why.

        Kept rather than deleted, for the reason phase 07 keeps dismissed review
        findings: resolved criticism stays visible, and a suggestion that
        vanished would be raised again the next time the habit recurred.
        """
        if not rejected_by:
            raise ValueError("rejected_by is required: an anonymous rejection is unreviewable")
        self._require_undecided(suggestion)

        suggestion.status = FindingStatus.REJECTED
        suggestion.decided_by = rejected_by
        suggestion.reason = reason
        suggestion.decided_at = self._recorder.clock()
        self.session.flush()

    def open_suggestions(self, user_id: str) -> tuple[VoiceSuggestion, ...]:
        """Everything still waiting for an answer."""
        return tuple(
            self.session.scalars(
                select(VoiceSuggestion)
                .where(
                    VoiceSuggestion.user_id == user_id,
                    VoiceSuggestion.status == FindingStatus.PROPOSED,
                )
                .order_by(VoiceSuggestion.created_at, VoiceSuggestion.id)
            )
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _instruction_for(self, pattern: EditPattern, category: VoiceCategory) -> VoiceInstruction:
        """The rule a pattern would become.

        A **strong preference**, never a hard rule. Promoting an inference to the
        one strength the system enforces would let three edits stop an article.
        The author can raise it afterwards; the system should not raise it on
        their behalf.
        """
        replacement = f" in favour of something like {pattern.added!r}" if pattern.added else ""
        return VoiceInstruction(
            id=pattern.instruction_id,
            category=category,
            strength=InstructionStrength.STRONG_PREFERENCE,
            text=f"Avoid {pattern.removed!r}{replacement}.",
            prohibits=(pattern.removed,),
            rationale=(
                f"inferred from {pattern.occurrences} edits in which you removed "
                f"{pattern.removed!r}"
            ),
        )

    def _evidence_for(self, pattern: EditPattern) -> dict[str, Any]:
        """The author's own sentences, so they can disagree with the inference.

        plan/10 asks for supporting examples, and the reason is that "you often
        replace X" is a claim while three of your own sentences are something you
        can look at and recognise — or not.
        """
        # Ordered explicitly. A set of ids comes back in whatever order the
        # database chooses, and examples shown to an author should read in the
        # order they wrote them — otherwise the same evidence looks different
        # each time it is presented.
        edits = (
            self.session.scalars(
                select(ManualEdit)
                .where(ManualEdit.id.in_(pattern.edit_ids))
                .order_by(ManualEdit.made_at, ManualEdit.id)
            ).all()
            if pattern.edit_ids
            else []
        )
        return {
            "edit_ids": list(pattern.edit_ids),
            "occurrences": pattern.occurrences,
            "examples": [{"before": edit.before, "after": edit.after} for edit in edits],
        }

    def _require_undecided(self, suggestion: VoiceSuggestion) -> None:
        if suggestion.is_decided:
            raise ValueError(
                f"suggestion {suggestion.id} was already {suggestion.status.value} by "
                f"{suggestion.decided_by or 'someone'}; deciding it twice would silently "
                "produce a second profile version"
            )


def _run_of(version: ArticleVersion, session: Session) -> provenance_models.PipelineRun:
    """The run an edit to ``version`` belongs to."""
    execution = session.get(provenance_models.StageExecution, version.created_by_execution_id)
    if execution is None:
        raise ValueError(
            f"article version {version.id} names no creating execution; an edit to it could "
            "not be attached to a run"
        )
    return execution.pipeline_run


def _words(text: str) -> list[str]:
    return [word.lower() for word in _WORD.findall(text)]


def _most_common(values: Sequence[str]) -> str:
    """The replacement that turns up most often, or nothing if none does."""
    if not values:
        return ""
    return max(sorted(set(values)), key=values.count)


__all__ = ["HABIT_THRESHOLD", "EditPattern", "VoiceLearning", "detect_edit_patterns"]
