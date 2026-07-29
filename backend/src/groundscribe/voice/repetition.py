"""Noticing when a voice has become a template (phase 10).

plan/10 → *detect structural sameness across recent articles (identical openings,
repeated section sequences, reused contrast patterns, similar conclusions,
repeated rhetorical devices, repeated cadence) and warn, without forcing a single
template.*

This module is the counterweight to the rest of the phase. Everything else here
makes the system better at writing like one person; done well, that is also how
it starts writing the same article every time. The profile cannot detect its own
overfitting — each article obeys it perfectly — so the check has to look *across*
articles, at the shapes a single article gives no reason to question.

Two deliberate limits:

- **It warns; it never edits.** A detector wired to a corrective action would be
  a second style system fighting the first, and the author is the only one who
  knows whether three pieces open the same way because the voice is stale or
  because they are three parts of a series.
- **It is arithmetic, not a model.** Sameness of *shape* is countable — the same
  four opening words, the same heading sequence, sentence lengths that never
  vary — and a judgement call here would produce warnings nobody could argue
  with, about writing nobody had read.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

#: How many articles are needed before sameness means anything. Two articles
#: that open alike are a coincidence; the smallest sample worth a warning is
#: three, and the caller may demand more.
MIN_SAMPLE = 3

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_WORD = re.compile(r"[a-z0-9']+")

#: Contrast constructions plan/10 names. Literal because they are how the pattern
#: is actually written; a general "contrast detector" would flag every sentence
#: containing "but".
_CONTRASTS: tuple[tuple[str, str], ...] = (
    ("not-x-but-y", r"\bnot\b[^.?!]{1,60}?,?\s+but\b"),
    ("isnt-its", r"\b(is|are|was|were)n't\b[^.?!]{1,60}?\bit'?s\b"),
    ("less-x-more-y", r"\bless\b[^.?!]{1,40}?\bmore\b"),
)

#: Rhetorical devices that read as personality once and as a tic four times.
_DEVICES: tuple[tuple[str, str], ...] = (
    ("rhetorical-question", r"\?\s*$"),
    ("here-is-the-thing", r"\bhere'?s the (thing|catch|problem)\b"),
    ("the-truth-is", r"\bthe (truth|reality) is\b"),
    ("and-that-is-the", r"\band that'?s the\b"),
)


#: How a group of same-shaped articles is described: the shared key, and how
#: many share it.
Describe = Callable[[str, int], str]


class RepetitionSignal(StrEnum):
    """The kinds of sameness plan/10 asks for, one member each."""

    OPENING = "opening"
    SECTION_SEQUENCE = "section_sequence"
    CONTRAST_PATTERN = "contrast_pattern"
    CONCLUSION = "conclusion"
    RHETORICAL_DEVICE = "rhetorical_device"
    CADENCE = "cadence"


@dataclass(frozen=True)
class SampledArticle:
    """One recent article, as the detector needs it."""

    id: str
    body: str


@dataclass(frozen=True)
class RepetitionFinding:
    """One shape several articles share.

    ``articles`` names them all. A warning that said "your openings repeat"
    without saying which ones would leave the author to find them, and the whole
    value here is being pointed at the evidence.
    """

    signal: RepetitionSignal
    detail: str
    articles: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.articles)


@dataclass(frozen=True)
class RepetitionThresholds:
    """How much sameness counts as too much.

    ``share`` is a fraction of the sample rather than a count, so the same
    setting means the same thing for four articles and for forty.

    ``cadence_spread`` is in words: if the mean sentence length of every article
    sits inside a band this narrow, the rhythm has stopped varying. Generous by
    default — prose naturally clusters, and a detector that fired on ordinary
    consistency would be ignored within a week, which is the only failure mode
    that matters for a warning.
    """

    share: float = 0.6
    minimum: int = MIN_SAMPLE
    cadence_spread: float = 1.5
    #: How much of the first sentence has to match. Three words, because the
    #: habit lives in the construction rather than the subject: "We put a cache
    #: …", "We put a rate limiter …" and "We put a queue …" are the same opening
    #: move, and a fourth word would separate them on the noun.
    opening_words: int = 3


def detect_repetition(
    articles: Sequence[SampledArticle],
    *,
    thresholds: RepetitionThresholds | None = None,
) -> tuple[RepetitionFinding, ...]:
    """Every shape too much of ``articles`` shares.

    Returns findings in signal order so two runs over the same sample read the
    same way. An empty result is the ordinary case and says nothing more than
    that: this looks for one specific failure, not for quality.
    """
    limits = thresholds or RepetitionThresholds()
    if len(articles) < limits.minimum:
        # Not "no repetition" — not enough to say. Silence is the honest answer
        # when the sample cannot support the claim either way.
        return ()

    needed = max(2, round(len(articles) * limits.share))
    findings: list[RepetitionFinding] = []
    findings.extend(_shared_openings(articles, limits, needed))
    findings.extend(_shared_sections(articles, needed))
    findings.extend(
        _shared_matches(articles, _CONTRASTS, RepetitionSignal.CONTRAST_PATTERN, needed)
    )
    findings.extend(_shared_conclusions(articles, limits, needed))
    findings.extend(_shared_matches(articles, _DEVICES, RepetitionSignal.RHETORICAL_DEVICE, needed))
    findings.extend(_shared_cadence(articles, limits))
    return tuple(sorted(findings, key=lambda finding: finding.signal.value))


# ----------------------------------------------------------------------
# The six signals
# ----------------------------------------------------------------------


def _shared_openings(
    articles: Sequence[SampledArticle], limits: RepetitionThresholds, needed: int
) -> list[RepetitionFinding]:
    """Articles whose first sentence starts the same way.

    Compared on the first few *words* rather than the whole sentence: two pieces
    opening "We put a read-through cache…" and "We put a rate limiter…" share the
    habit, and comparing whole sentences would miss it.
    """
    return _grouped(
        {
            article.id: " ".join(_words(_first_sentence(article.body))[: limits.opening_words])
            for article in articles
        },
        signal=RepetitionSignal.OPENING,
        needed=needed,
        describe=lambda key, count: f"{count} articles open on {key!r}",
    )


def _shared_conclusions(
    articles: Sequence[SampledArticle], limits: RepetitionThresholds, needed: int
) -> list[RepetitionFinding]:
    """Articles that land the same way."""
    return _grouped(
        {
            article.id: " ".join(_words(_last_sentence(article.body))[: limits.opening_words])
            for article in articles
        },
        signal=RepetitionSignal.CONCLUSION,
        needed=needed,
        describe=lambda key, count: f"{count} articles close on {key!r}",
    )


def _shared_sections(articles: Sequence[SampledArticle], needed: int) -> list[RepetitionFinding]:
    """Articles with the same sequence of section headings.

    The headings' *first words*, joined. Identical wording would be an obvious
    problem nobody needs a detector for; the interesting case is three articles
    that all go "Why it was slow → What we tried → What I would do differently".
    """
    shapes = {}
    for article in articles:
        headings = [_words(heading)[:2] for heading in _HEADING.findall(article.body)]
        if len(headings) >= 2:
            shapes[article.id] = " → ".join(" ".join(words) for words in headings)
    return _grouped(
        shapes,
        signal=RepetitionSignal.SECTION_SEQUENCE,
        needed=needed,
        describe=lambda key, count: f"{count} articles run the same sections: {key}",
    )


def _shared_matches(
    articles: Sequence[SampledArticle],
    patterns: tuple[tuple[str, str], ...],
    signal: RepetitionSignal,
    needed: int,
) -> list[RepetitionFinding]:
    """Constructions that appear in too many of the articles."""
    findings = []
    for name, expression in patterns:
        regex = re.compile(expression, re.IGNORECASE | re.MULTILINE)
        matched = tuple(article.id for article in articles if regex.search(article.body))
        if len(matched) >= needed:
            findings.append(
                RepetitionFinding(
                    signal=signal,
                    detail=f"the {name.replace('-', ' ')} construction recurs",
                    articles=matched,
                )
            )
    return findings


def _shared_cadence(
    articles: Sequence[SampledArticle], limits: RepetitionThresholds
) -> list[RepetitionFinding]:
    """Prose whose rhythm has stopped varying between articles.

    Every article, not most: cadence is the weakest of the six signals, and a
    finding that fired on a subset would mostly report that two pieces happened
    to be about equally dense.
    """
    means = {article.id: _mean_sentence_length(article.body) for article in articles}
    lengths = [value for value in means.values() if value > 0]
    if len(lengths) < len(articles) or len(lengths) < 2:
        return []

    spread = max(lengths) - min(lengths)
    if spread > limits.cadence_spread:
        return []
    return [
        RepetitionFinding(
            signal=RepetitionSignal.CADENCE,
            detail=(
                f"every article averages {statistics.mean(lengths):.0f} words a sentence "
                f"(spread {spread:.1f}); the rhythm has stopped varying"
            ),
            articles=tuple(means),
        )
    ]


# ----------------------------------------------------------------------
# Shared machinery
# ----------------------------------------------------------------------


def _grouped(
    keyed: dict[str, str],
    *,
    signal: RepetitionSignal,
    needed: int,
    describe: Describe,
) -> list[RepetitionFinding]:
    """Findings for every key too many articles share."""
    counts = Counter(key for key in keyed.values() if key)
    findings = []
    for key, count in counts.items():
        if count >= needed:
            findings.append(
                RepetitionFinding(
                    signal=signal,
                    detail=describe(key, count),
                    articles=tuple(article for article, value in keyed.items() if value == key),
                )
            )
    return findings


def _sentences(body: str) -> list[str]:
    return [sentence.strip() for sentence in _SENTENCE.split(_prose(body)) if sentence.strip()]


def _prose(body: str) -> str:
    """The body without its headings, which are not sentences."""
    return _HEADING.sub("", body)


def _first_sentence(body: str) -> str:
    sentences = _sentences(body)
    return sentences[0] if sentences else ""


def _last_sentence(body: str) -> str:
    sentences = _sentences(body)
    return sentences[-1] if sentences else ""


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _mean_sentence_length(body: str) -> float:
    lengths = [len(_words(sentence)) for sentence in _sentences(body)]
    return statistics.mean(lengths) if lengths else 0.0


__all__ = [
    "MIN_SAMPLE",
    "RepetitionFinding",
    "RepetitionSignal",
    "RepetitionThresholds",
    "SampledArticle",
    "detect_repetition",
]
