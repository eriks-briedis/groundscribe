"""How far the author moved the article before publishing it (phase 12).

plan/12 → *Manual edit distance: difference between the pipeline's proposed final
article and the user-approved article (character-level, sentence add/remove,
structural changes, claim changes, voice corrections) as a quality signal (high
score + heavy editing → weak rubric)*.

Everything else the pipeline knows about quality it learned from itself: a rubric
this repository ships, scoring prose a model wrote, on dimensions a model was
asked about. The author's own edits are the one judgement that came from outside
that loop, which is why they are worth measuring even though they are the hardest
thing here to measure well.

**Five numbers, not one.** A reworded sentence, a deleted claim and a moved
heading are three different problems with three different fixes. A single
"distance" would rise for all of them and explain none, and the aggregate an
experiment is compared on would become a figure nobody could act on.

**Deterministic and explainable, deliberately.** No model is asked what an edit
"meant". Every measure here is arithmetic over the two texts and the artefacts
the pipeline already has — the source claims, the active voice profile — so a
flagged rubric can be argued with by looking at the same sentences the measure
looked at.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: The fraction of an article a person has to rewrite before the edit is "heavy".
#:
#: A fifth. Below it, an author tightening prose is doing the ordinary last pass
#: every published thing gets; above it they are disagreeing with the draft. The
#: number is a judgement and it is written down here rather than inferred so that
#: revising it is a visible change to the signal rather than a drift in it.
HEAVY_EDIT_RATIO = 0.20

#: How alike two sentences must be to count as the same sentence, reworded.
#:
#: Measured over words rather than characters: two unrelated English sentences
#: share a surprising number of letters and almost no words, so the word-level
#: ratio separates "reworded" from "replaced" where the character-level one
#: leaves them overlapping.
SENTENCE_MATCH_RATIO = 0.6

#: How much of a claim has to survive in the prose for the article to still rest
#: on it. Half its significant words — a threshold low enough to see through
#: paraphrase and high enough that an incidental word in common is not evidence.
CLAIM_PRESENCE_RATIO = 0.5

#: Words too common to be evidence that a claim is still being made.
# fmt: off
_STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "how", "in", "into", "is", "it", "its", "of", "on", "or",
    "so", "than", "that", "the", "their", "then", "there", "these", "this",
    "to", "was", "were", "when", "which", "who", "why", "will", "with",
])
# fmt: on

#: Words and figures. Digits are included because a claim is often mostly
#: numbers, and a tokeniser that dropped them would find every claim absent.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+\.)\s+")
_FENCE = re.compile(r"^\s*```")
_QUOTE = re.compile(r"^\s*>")


class ManualEditDistance(BaseModel):
    """What one person changed between the proposed article and the published one.

    ``characters`` sizes the edit; the other four say what kind of edit it was.
    Only the first feeds :attr:`heavy`, because "how much was rewritten" is a
    question about magnitude and the rest are questions about meaning — a single
    deleted claim is serious without being large, and reporting it as a large
    edit would be a different, wrong claim.
    """

    model_config = ConfigDict(frozen=True)

    characters: int = Field(ge=0)
    character_ratio: float = Field(ge=0.0, le=1.0)
    sentences_added: int = Field(ge=0)
    sentences_removed: int = Field(ge=0)
    structural_changes: int = Field(ge=0)
    claim_changes: int = Field(ge=0)
    voice_corrections: int = Field(ge=0)

    @property
    def heavy(self) -> bool:
        """Whether the author rewrote enough of it to disagree with the draft."""
        return self.character_ratio >= HEAVY_EDIT_RATIO

    @property
    def untouched(self) -> bool:
        """Whether the author published exactly what they were handed."""
        return self.characters == 0 and self.structural_changes == 0


@dataclass(frozen=True)
class RubricSignal:
    """What one manual edit says about the rubric that scored the article.

    ``detail`` carries the numbers rather than a verdict alone: this signal is
    read by a person deciding whether to revise a rubric version, and "the rubric
    may be weak" without the score and the ratio behind it is an accusation they
    cannot check.
    """

    weak_rubric: bool
    detail: str


def measure_manual_edit(
    proposed: str,
    approved: str,
    *,
    claims: Sequence[str] = (),
    prohibited: Sequence[str] = (),
) -> ManualEditDistance:
    """Measure the five differences between what was proposed and what was approved.

    ``claims`` are the source claims the article was written from and
    ``prohibited`` the terms the active voice profile forbids. Both default to
    empty and their measures then read zero — a caller who has neither gets the
    three measures that need only the two texts, rather than a guess at the other
    two.
    """
    characters = _character_distance(proposed, approved)
    added, removed = _sentence_movement(proposed, approved)
    return ManualEditDistance(
        characters=characters,
        character_ratio=min(1.0, characters / max(len(proposed), 1)),
        sentences_added=added,
        sentences_removed=removed,
        structural_changes=_structural_changes(proposed, approved),
        claim_changes=_claim_changes(proposed, approved, claims),
        voice_corrections=_voice_corrections(proposed, approved, prohibited),
    )


def rubric_signal(
    distance: ManualEditDistance, *, overall: float, threshold: float
) -> RubricSignal:
    """Whether a score and the edits that followed it disagree.

    plan/12 → *high score + heavy editing → weak rubric*. One direction only. A
    poor score followed by a rewrite is the rubric working, and flagging it would
    mean the loudest output of this measure came from the cases the system got
    right — after which nobody would read the flag that matters.
    """
    if overall < threshold:
        return RubricSignal(
            weak_rubric=False,
            detail=(
                f"scored {overall:g}, below the {threshold:g} the rubric asks for: "
                "the rubric and the author agree the draft needed work"
            ),
        )
    if not distance.heavy:
        return RubricSignal(
            weak_rubric=False,
            detail=(
                f"scored {overall:g} and the author changed "
                f"{distance.character_ratio:.0%} of it by hand"
            ),
        )
    return RubricSignal(
        weak_rubric=True,
        detail=(
            f"scored {overall:g}, at or above the {threshold:g} the rubric asks for, and the "
            f"author then rewrote {distance.character_ratio:.0%} of it by hand — "
            f"{distance.sentences_added} sentence(s) added, "
            f"{distance.sentences_removed} removed, "
            f"{distance.structural_changes} structural change(s), "
            f"{distance.claim_changes} claim change(s), "
            f"{distance.voice_corrections} voice correction(s)"
        ),
    )


# ----------------------------------------------------------------------
# The measures
# ----------------------------------------------------------------------


def _character_distance(before: str, after: str) -> int:
    """How many characters differ, counted only inside the lines that changed.

    Two passes rather than one. A character diff over two whole articles is
    quadratic in their length for an answer that only ever concerns the parts
    that moved, so the line diff finds those parts and the character diff runs
    inside them. The result is the same number for any realistic edit and does
    not degrade with the length of the article the edit sits in.
    """
    before_lines, after_lines = before.splitlines(), after.splitlines()
    total = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, before_lines, after_lines
    ).get_opcodes():
        if tag == "equal":
            continue
        total += _opcode_distance(
            difflib.SequenceMatcher(
                None, "\n".join(before_lines[i1:i2]), "\n".join(after_lines[j1:j2]), autojunk=False
            )
        )
    return total


def _sentence_movement(before: str, after: str) -> tuple[int, int]:
    """How many sentences the author supplied, and how many they cut.

    Matched greedily and one-to-one: each approved sentence claims its closest
    surviving counterpart, and whatever is left over in the proposal was removed.
    Rewording therefore costs nothing here — a reworded sentence still matches
    itself — which is the distinction plan/12 draws by asking for *add/remove*
    rather than for a count of sentences that changed.
    """
    unclaimed = _sentences(before)
    added = 0
    for sentence in _sentences(after):
        index = _closest(sentence, unclaimed)
        if index is None:
            added += 1
            continue
        unclaimed.pop(index)
    return added, len(unclaimed)


def _structural_changes(before: str, after: str) -> int:
    """How much of the article's shape moved, ignoring the words inside it.

    The outline is the sequence of blocks — headings with their level and text,
    and a marker for every other kind of block. Heading text is part of the
    outline because renaming a section is a structural act; paragraph text is not,
    because rewriting a paragraph is what the other measures are for.
    """
    return _opcode_distance(difflib.SequenceMatcher(None, _outline(before), _outline(after)))


def _claim_changes(before: str, after: str, claims: Sequence[str]) -> int:
    """How many source claims the article started or stopped resting on.

    Presence is judged by how much of the claim's significant vocabulary survives
    in the prose, so a paraphrase still counts as the claim being made. Both
    directions are counted: an author *adding* a claim the pipeline left out is
    as much a finding about the draft as one cutting a claim it invented.
    """
    return sum(1 for claim in claims if _states(before, claim) != _states(after, claim))


def _voice_corrections(before: str, after: str, prohibited: Sequence[str]) -> int:
    """How many prohibited terms the author had to take out themselves.

    Counted per instance removed rather than per term: a profile the voice pass
    ignored three times in one article is a worse failure than one it ignored
    once, and a per-term count would report them identically.

    Terms the author left in place are not counted. The measure is about their
    judgement, not about the profile's — an author who kept a word the profile
    forbids has made a decision, not a correction.
    """
    was, now = _token_counts(before), _token_counts(after)
    return sum(max(0, was.get(term.lower(), 0) - now.get(term.lower(), 0)) for term in prohibited)


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _opcode_distance(matcher: difflib.SequenceMatcher[Any]) -> int:
    """The size of everything that is not equal, in whichever side is longer.

    The same definition the run-comparison read uses for its line distance, so a
    reader who has learned to interpret one number can read the other.
    """
    return sum(
        max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
    )


def _sentences(text: str) -> list[str]:
    """The article as sentences, never crossing a line boundary.

    A heading and the paragraph beneath it are not one sentence merely because
    the heading has no full stop, and letting a sentence span the gap would make
    every heading edit look like a rewritten paragraph.
    """
    return [
        sentence
        for line in text.splitlines()
        if line.strip()
        for sentence in _SENTENCE_END.split(line.strip())
        if sentence.strip()
    ]


def _closest(sentence: str, candidates: Sequence[str]) -> int | None:
    """Which candidate is this sentence a rewording of, if any."""
    words = _tokens(sentence)
    best_index, best_ratio = None, SENTENCE_MATCH_RATIO
    for index, candidate in enumerate(candidates):
        ratio = difflib.SequenceMatcher(None, words, _tokens(candidate)).ratio()
        if ratio >= best_ratio:
            best_index, best_ratio = index, ratio
    return best_index


def _outline(text: str) -> list[str]:
    """One marker per block: what kind it is, and for a heading, what it says."""
    markers: list[str] = []
    in_paragraph = False
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            markers.append("code")
            in_paragraph = False
            continue
        if fenced:
            continue
        if not line.strip():
            in_paragraph = False
            continue
        heading = _HEADING.match(line.strip())
        if heading is not None:
            markers.append(f"h{len(heading.group(1))}:{heading.group(2).strip().lower()}")
            in_paragraph = False
            continue
        if _LIST_ITEM.match(line):
            markers.append("list")
            in_paragraph = False
            continue
        if _QUOTE.match(line):
            markers.append("quote")
            in_paragraph = False
            continue
        if not in_paragraph:
            markers.append("para")
            in_paragraph = True
    return markers


def _states(text: str, claim: str) -> bool:
    """Whether ``text`` still makes ``claim``, by how much of it survives."""
    significant = {word for word in _tokens(claim) if word not in _STOPWORDS}
    if not significant:
        return False
    present = significant & set(_tokens(text))
    return len(present) / len(significant) >= CLAIM_PRESENCE_RATIO


def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN.finditer(text)]


def _token_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in _tokens(text):
        counts[token] = counts.get(token, 0) + 1
    return counts


__all__ = [
    "CLAIM_PRESENCE_RATIO",
    "HEAVY_EDIT_RATIO",
    "SENTENCE_MATCH_RATIO",
    "ManualEditDistance",
    "RubricSignal",
    "measure_manual_edit",
    "rubric_signal",
]
