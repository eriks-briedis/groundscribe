"""The deterministic final-validation checks (phase 08).

plan/08 → *ValidateFinalOutput stage*: fourteen predicates over an article version
and the contract it was written against. Every one of them is a pure function of
its input. No model is consulted, and that is the point rather than an economy —
this is the last gate before publication, a validator that could rephrase could
also introduce, and a failure the author cannot re-run and reproduce is a failure
they have to take on faith.

The consequence is that some things the spec would like checked are *not* checked
here. The brief reserves material that may be stated but not developed, and
"developed" is a judgement; substantive review makes it, and this module looks
only for the reserved text appearing verbatim. Claiming otherwise would produce
failures nobody could confirm, which is worse than a gap everyone can see.

Two of plan/08's fourteen are not about the prose at all. Whether the version being
published is the version that passed review, and whether its stored bytes still
hash to what was recorded, are questions no amount of reading the article would
answer — and they are the two failures with the worst consequences.

A finding may carry a :class:`SafeCorrection`: a fix that changes no word of the
article. Renumbering a skipped heading level and deleting an internal annotation
both qualify; everything else fails instead, because plan/08's "never creatively
rewrite" is only meaningful if the line is drawn somewhere a reader can see it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from groundscribe.domain.enums import IssueSeverity
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.stages.schemas import ArticleBriefDocument, ArticleDraft
from groundscribe.workflow.policy import FailureCategory

#: How far from the brief's target length an article may land. Generous, because
#: the target is a brief's estimate rather than a measurement, and a validator
#: that argued about 5% would be enforcing a precision the brief never had.
LENGTH_TOLERANCE = 0.25

#: Placeholder markers a draft leaves behind. Matched inside brackets so ordinary
#: prose using the word "todo" is not a publication failure.
_PLACEHOLDER = re.compile(r"\[\s*(UNRESOLVED|TODO|TBD|FIXME|XXX|PLACEHOLDER)\b[^\]]*\]", re.I)

#: Annotations that exist for the trace or the source and must never publish.
_ANNOTATION = re.compile(r"<!--.*?-->|\[\s*(?:SOURCE|TRACE|NOTE|REF)\s*:[^\]]*\]", re.S | re.I)

#: Markdown links. Deliberately not a full parser: an empty target or empty text
#: is the failure that survives review, because both render as something that
#: looks like a link and does nothing.
_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")

_FENCE = re.compile(r"^\s*```", re.M)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_QUOTED = re.compile(r"[“\"][^”\"\n]{4,}[”\"]")

#: Defining a thing by what it is not: "It is not X. It is Y", "not X, but Y".
#:
#: A pattern rather than a literal, which is why it lives here and not in the
#: voice profile: a hard rule there must name strings a person can read
#: (``voice/schemas.py``), and this has no literal form. The profile pushes the
#: model away from it; this counts what actually came out.
_CONTRAST = re.compile(
    r"\b(?:is|are|was|were)\s+not\b[^.;]{0,90}[.;]\s*(?:It|They|That|This)\s+(?:is|are)\b"
    r"|\bnot\s+[^.,;]{2,60},\s*but\b"
    r"|\bnot\b[^.;]{0,80};\s*it\s+is\b"
    r"|\b(?:is|are)\s+not\s+(?:merely|just|simply|only)\b",
    re.I,
)

#: A comma series of five or more items ending in "and X".
_LONG_SERIES = re.compile(r"\b\w[\w-]*(?:,\s+[\w][\w -]*){3,},?\s+and\s+\w")

#: How many contrast constructions per hundred sentences is too many.
#:
#: Three. The article that produced this check ran at 5.6 — thirteen in a hundred
#: and forty-two sentences, in a piece arguing against generated-prose habits —
#: and a reader registers the rhythm long before the argument. Three leaves room
#: for the construction where two things are genuinely being distinguished, which
#: is what it is for, and refuses it as a cadence.
MAX_CONTRAST_PER_HUNDRED = 3.0

#: The fewest items that make a comma series a list nobody retains.
#:
#: Five. The voice profile asks for three; this is the point past which it stops
#: being a matter of taste. The measured article had twelve of them, several
#: repeating the same eight nouns in a different order.
MAX_SERIES_ITEMS = 5

#: The fewest concrete tokens per thousand words an article should carry.
#:
#: Six. Measured against a two-thousand-word article about a system with states,
#: scores and thresholds that contained **no numbers and no code spans at all** —
#: it scored 3.9 on quoted phrases alone. An article claiming a thing is
#: inspectable and never showing anything inspected has argued against itself,
#: and that is checkable without asking a model what it thinks.
MIN_SPECIFICS_PER_THOUSAND = 6.0

#: Words too common to mean anything when a title and a thesis share them.
# fmt: off
_STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "how", "in", "into", "is", "it", "its", "of", "on", "or", "so", "that",
    "the", "their", "then", "there", "these", "this", "to", "was", "what",
    "when", "which", "who", "why", "will", "with", "you", "your",
])
# fmt: on


class ValidationCheck(StrEnum):
    """The fourteen checks plan/08 names, and three phase 16 added.

    A closed vocabulary because the report lists which checks *ran*, not only
    which objected. A validator that quietly stopped performing one would
    otherwise be indistinguishable from an article that kept passing it.
    """

    CONFIDENTIAL_NAMES = "confidential_names"
    #: Phase 13's addition, and a different question from the one above: that
    #: check knows a list of names the project holds confidential, this one knows
    #: which spans of the *source* were flagged out of the final output.
    EXCLUDED_MATERIAL = "excluded_material"
    PROHIBITED_TERMINOLOGY = "prohibited_terminology"
    UNRESOLVED_PLACEHOLDERS = "unresolved_placeholders"
    REQUIRED_FACTS = "required_facts"
    UNSUPPORTED_NUMBERS = "unsupported_numbers"
    TITLE_MATCHES_THESIS = "title_matches_thesis"
    PLATFORM_FORMATTING = "platform_formatting"
    LENGTH_IN_RANGE = "length_in_range"
    VALID_MARKDOWN = "valid_markdown"
    VALID_LINKS = "valid_links"
    RESERVED_MATERIAL = "reserved_material"
    INTERNAL_ANNOTATIONS = "internal_annotations"
    EXPORTED_VERSION = "exported_version"
    CONTENT_HASH = "content_hash"
    #: Phase 16's three, and the only ones here that plan/08 did not name. They
    #: come from a critique of an article this pipeline actually published, and
    #: each measures a habit the voice profile pushes against but cannot check:
    #: a pattern has no literal form, and a hard rule must name literals.
    CONTRAST_DENSITY = "contrast_density"
    LIST_LENGTH = "list_length"
    CONCRETE_DETAIL = "concrete_detail"


@dataclass(frozen=True)
class SafeCorrection:
    """A fix the validator may apply itself.

    "Safe" has one test and it is checkable: the correction changes no word of the
    article. Renumbering a heading and deleting an annotation both pass it;
    rewording a sentence never could, whatever the reason.
    """

    before: str
    after: str
    reason: str


@dataclass(frozen=True)
class ValidationFinding:
    """One check's objection, and what it would take to resolve it."""

    check: ValidationCheck
    detail: str
    severity: IssueSeverity = IssueSeverity.BLOCKING
    passage: str = ""
    suggested_route: FailureCategory | None = None
    correction: SafeCorrection | None = None


@dataclass(frozen=True)
class ValidationInput:
    """Everything the checks read.

    ``source_text`` is the source model rendered to text rather than the model
    itself: the only question asked of it is whether a figure appears anywhere in
    the material, and a check that walked the model's structure would have to be
    taught about every field added to it.
    """

    draft: ArticleDraft
    brief: ArticleBriefDocument
    source_text: str
    constraints: EditorialConstraints
    prohibited_terms: Sequence[str] = ()
    #: The version that passed review, when the caller knows it. ``None`` skips
    #: the check rather than failing it — the predicate needs two things to
    #: compare and the stage is what supplies the second.
    version_id: str | None = None
    passed_version_id: str | None = None
    #: Whether the stored bytes still hash to what was recorded. ``None`` when
    #: nothing was asked to verify them.
    hash_verified: bool | None = None
    reserved_material: Sequence[str] = field(default=())
    #: Spans of source material flagged out of the final output (phase 13).
    #: Empty for a project that flagged nothing, which is the common case and
    #: costs the check nothing.
    excluded_material: Sequence[str] = field(default=())


def run_checks(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """Every objection, in the order the checks are declared.

    All of them run; none short-circuits. A person deciding what to do about a
    failed validation needs the whole list — one wrong number is a different
    problem from an article that is also too long, badly linked and still carrying
    a placeholder — and phase 08's routing picks a destination from them.
    """
    return tuple(
        finding
        for check in (
            _confidential_names,
            _excluded_material,
            _prohibited_terminology,
            _unresolved_placeholders,
            _required_facts,
            _unsupported_numbers,
            _title_matches_thesis,
            _platform_formatting,
            _length_in_range,
            _valid_markdown,
            _valid_links,
            _reserved_material,
            _internal_annotations,
            _exported_version,
            _content_hash,
            _contrast_density,
            _list_length,
            _concrete_detail,
        )
        for finding in check(article)
    )


def _confidential_names(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: no confidential names. The one failure that cannot be taken back."""
    text = f"{article.draft.title}\n{article.draft.body}"
    found = [name for name in article.constraints.confidential_names if name and name in text]
    if not found:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.CONFIDENTIAL_NAMES,
            detail=f"the article names {', '.join(found)}, which this project holds confidential",
            suggested_route=FailureCategory.SUBSTANTIVE_ISSUE,
        ),
    )


def _excluded_material(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/13: source flagged out of the final output must not appear in it.

    Verbatim, and only verbatim. A paraphrase gets through — stated here rather
    than papered over, because this validator calls no model, every finding it
    raises has to be one an author can reproduce by reading, and a fuzzy matcher
    would raise failures nobody could confirm while still missing the determined
    case. Verbatim reuse is the failure that actually happens: a sentence copied
    out of the source while drafting.

    The finding names where the material came from rather than repeating it. The
    report is stored and exported, so a finding that quoted the leak would move
    it into the document written to complain about it.
    """
    text = f"{article.draft.title}\n{article.draft.body}"
    found = [span for span in article.excluded_material if span and span in text]
    if not found:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.EXCLUDED_MATERIAL,
            detail=(
                f"the article reproduces {len(found)} passage(s) of source material flagged "
                "as excluded from the final output; the longest is "
                f"{max(len(span) for span in found)} characters"
            ),
            suggested_route=FailureCategory.SUBSTANTIVE_ISSUE,
        ),
    )


def _prohibited_terminology(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: no prohibited terminology.

    Case-insensitive, because a phrase forbidden in the middle of a sentence is
    still forbidden at the start of one.
    """
    body = article.draft.body.casefold()
    found = [term for term in article.prohibited_terms if term and term.casefold() in body]
    if not found:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.PROHIBITED_TERMINOLOGY,
            detail=f"the article uses prohibited terminology: {', '.join(found)}",
            severity=IssueSeverity.MINOR,
            suggested_route=FailureCategory.STYLE_ISSUE,
        ),
    )


def _unresolved_placeholders(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: no unresolved placeholders.

    Two sources, and both are needed. The bracket pattern catches what a writer
    typed; the draft's own ``unresolved`` list catches a marker the drafter
    declared in whatever shape it chose. A marker is a hole left deliberately, and
    publishing it publishes the hole.
    """
    markers = [item.marker for item in article.draft.unresolved]
    markers.extend(match.group(0) for match in _PLACEHOLDER.finditer(article.draft.body))
    if not markers:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.UNRESOLVED_PLACEHOLDERS,
            detail=f"unresolved markers remain in the prose: {', '.join(sorted(set(markers)))}",
            passage=markers[0],
            suggested_route=FailureCategory.FACTUAL_GAP,
        ),
    )


def _required_facts(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: required facts present.

    "Required" is the brief's word, not a guess: every claim a *mandatory* section
    names has to be one the article actually argued from. An optional section that
    was dropped took its claims with it, legitimately.
    """
    required = {
        claim_id
        for section in article.brief.argument_structure
        if section.mandatory
        for claim_id in section.claim_ids
    }
    missing = sorted(required - set(article.draft.claims_used))
    if not missing:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.REQUIRED_FACTS,
            detail=(
                f"the brief's mandatory sections require {', '.join(missing)}, which the "
                "article does not use"
            ),
            suggested_route=FailureCategory.SUBSTANTIVE_ISSUE,
        ),
    )


def _unsupported_numbers(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: no unsupported numbers introduced.

    The one invention check a deterministic validator can honestly make. Prose can
    be paraphrased past any comparison, but a figure either appears somewhere in
    the source material or arrived between there and here.

    Code blocks are excluded: the numbers in a code example belong to the code,
    and requiring the source to contain them would fail every article that shows
    one.
    """
    body = _without_code(article.draft.body)
    invented = sorted(
        {
            match.group(0)
            for match in _NUMBER.finditer(body)
            if match.group(0) not in article.source_text
        }
    )
    if not invented:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.UNSUPPORTED_NUMBERS,
            detail=(
                f"the article states {', '.join(invented)}, which the source material "
                "never contains"
            ),
            suggested_route=FailureCategory.FACTUAL_GAP,
        ),
    )


def _title_matches_thesis(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: title matches thesis.

    Deliberately weak: one significant word in common. A stronger rule would be a
    judgement about meaning, which is not this stage's to make — and the failure
    worth catching is blunt anyway. A retitled article whose title now promises a
    different piece shares nothing with the argument beneath it.
    """
    title_words = _significant(article.draft.title)
    thesis_words = _significant(article.draft.thesis) | _significant(article.brief.thesis)
    if not title_words or title_words & thesis_words:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.TITLE_MATCHES_THESIS,
            detail=(
                f"the title {article.draft.title!r} shares no significant word with the "
                "thesis; a title promising a different article is the first thing a reader reads"
            ),
            severity=IssueSeverity.MAJOR,
            suggested_route=FailureCategory.SUBSTANTIVE_ISSUE,
        ),
    )


def _platform_formatting(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: formatting matches the platform.

    A skipped heading level is the correctable case: every renderer builds its
    outline from the levels, so ``##`` followed by ``####`` produces a document
    whose structure is wrong everywhere it is read. Renumbering it changes no word,
    so the finding carries the fix rather than the failure.
    """
    findings: list[ValidationFinding] = []
    previous = 0
    for match in _HEADING.finditer(_without_code(article.draft.body)):
        level = len(match.group(1))
        if previous and level > previous + 1:
            corrected = "#" * (previous + 1) + " " + match.group(2)
            findings.append(
                ValidationFinding(
                    check=ValidationCheck.PLATFORM_FORMATTING,
                    detail=(
                        f"heading {match.group(2)!r} is level {level} under a level "
                        f"{previous}; the outline skips a level"
                    ),
                    severity=IssueSeverity.MINOR,
                    passage=match.group(0),
                    suggested_route=FailureCategory.STYLE_ISSUE,
                    correction=SafeCorrection(
                        before=match.group(0),
                        after=corrected,
                        reason="renumbered to the next level down; no word of the heading changed",
                    ),
                )
            )
            level = previous + 1
        previous = level
    return tuple(findings)


def _length_in_range(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: length within range of what the brief asked for."""
    target = article.brief.target_length_words
    words = article.draft.word_count
    low, high = target * (1 - LENGTH_TOLERANCE), target * (1 + LENGTH_TOLERANCE)
    if low <= words <= high:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.LENGTH_IN_RANGE,
            detail=(
                f"the article is {words} words against a target of {target} "
                f"(permitted {low:.0f}-{high:.0f})"
            ),
            severity=IssueSeverity.MAJOR,
            suggested_route=FailureCategory.SUBSTANTIVE_ISSUE,
        ),
    )


def _valid_markdown(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: valid Markdown.

    One structural failure rather than a parser: an unbalanced code fence. It is
    the one that silently swallows the rest of the article on every renderer, and
    the one a reader of the source would not notice.
    """
    fences = len(_FENCE.findall(article.draft.body))
    if fences % 2 == 0:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.VALID_MARKDOWN,
            detail=(
                f"{fences} code fences: one is unclosed, and everything after it renders as code"
            ),
            suggested_route=FailureCategory.STYLE_ISSUE,
        ),
    )


def _valid_links(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: valid links and references."""
    broken = [
        match.group(0)
        for match in _LINK.finditer(article.draft.body)
        if not match.group(2).strip() or not match.group(1).strip()
    ]
    if not broken:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.VALID_LINKS,
            detail=f"links with no target or no text: {', '.join(broken)}",
            severity=IssueSeverity.MAJOR,
            passage=broken[0],
            suggested_route=FailureCategory.STYLE_ISSUE,
        ),
    )


def _reserved_material(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: no reserved-material leak.

    Verbatim only, and the limitation is deliberate. The brief reserves material
    that may be stated but not developed; "developed" is a judgement, substantive
    review makes it, and a deterministic validator claiming to have made it would
    produce a failure the author could not confirm.
    """
    reserved = list(article.reserved_material) or list(article.brief.reserved_material)
    found = [item for item in reserved if item and item in article.draft.body]
    if not found:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.RESERVED_MATERIAL,
            detail=f"material the brief reserved appears verbatim: {'; '.join(found)}",
            severity=IssueSeverity.MAJOR,
            passage=found[0],
            suggested_route=FailureCategory.SUBSTANTIVE_ISSUE,
        ),
    )


def _internal_annotations(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: no trace-only or source-only annotations remain.

    Correctable, because the text was never part of the article: an HTML comment
    or a ``[SOURCE: …]`` note exists for the pipeline's benefit and deleting it
    leaves every word of the prose where it was.
    """
    return tuple(
        ValidationFinding(
            check=ValidationCheck.INTERNAL_ANNOTATIONS,
            detail=f"an internal annotation remains in the prose: {match.group(0)!r}",
            passage=match.group(0),
            suggested_route=FailureCategory.STYLE_ISSUE,
            correction=SafeCorrection(
                before=match.group(0),
                after="",
                reason="an annotation was never part of the article; removing it changes no prose",
            ),
        )
        for match in _ANNOTATION.finditer(article.draft.body)
    )


def _exported_version(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: the exported version is the version that passed review.

    Skipped rather than failed when the caller supplies only one of the two: the
    predicate needs both to compare, and a check that failed for lack of an input
    would make every partial call look like a tampered article.
    """
    if article.version_id is None or article.passed_version_id is None:
        return ()
    if article.version_id == article.passed_version_id:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.EXPORTED_VERSION,
            detail=(
                f"version {article.version_id} is being validated, but {article.passed_version_id} "
                "is the version that passed review; a report about one is not a report about "
                "the other"
            ),
        ),
    )


def _content_hash(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """plan/08: the artefact matches its recorded content hash."""
    if article.hash_verified is not False:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.CONTENT_HASH,
            detail=(
                "the stored bytes do not hash to what was recorded for them; this version "
                "has changed since it was written and nothing about reading it would say so"
            ),
        ),
    )


def _contrast_density(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """phase 16: the article does not define things by what they are not.

    Counted rather than forbidden. One or two are how a writer distinguishes two
    things a reader would otherwise confuse; a dozen is a cadence, and a reader
    hears it before they hear the argument. Only a count can tell those apart,
    which is why this is here and not a rule in the voice profile.

    Routed to ``style_issue`` so the voice pass is what corrects it. Nothing
    about the article's claims is wrong.
    """
    prose = _prose_only(article.draft.body)
    sentences = [item for item in _SENTENCE.split(prose) if item.strip()]
    found = _CONTRAST.findall(prose)
    if not sentences or not found:
        return ()
    density = 100 * len(found) / len(sentences)
    if density <= MAX_CONTRAST_PER_HUNDRED:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.CONTRAST_DENSITY,
            detail=(
                f"the article defines things by what they are not {len(found)} times in "
                f"{len(sentences)} sentences ({density:.1f} per hundred, above "
                f"{MAX_CONTRAST_PER_HUNDRED:g}); it has become the rhythm rather than a "
                "distinction"
            ),
            severity=IssueSeverity.MAJOR,
            passage=str(found[0])[:120],
            suggested_route=FailureCategory.STYLE_ISSUE,
        ),
    )


def _list_length(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """phase 16: no comma series long enough that nobody retains it.

    Five items is where a list stops carrying its own contents. The measured
    article had twelve such runs, several of them the same eight nouns in a
    different order — which reads as thoroughness and lands as noise.
    """
    prose = _prose_only(article.draft.body)
    runs = [match.group(0) for match in _LONG_SERIES.finditer(prose)]
    if not runs:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.LIST_LENGTH,
            detail=(
                f"{len(runs)} comma series of {MAX_SERIES_ITEMS} items or more; replace each "
                "with the two that matter, or with one example that implies the rest"
            ),
            severity=IssueSeverity.MINOR,
            passage=runs[0][:120],
            suggested_route=FailureCategory.STYLE_ISSUE,
        ),
    )


def _concrete_detail(article: ValidationInput) -> tuple[ValidationFinding, ...]:
    """phase 16: the article shows something, not only describes it.

    Concrete means countable here: a number, an inline code span, a quoted
    phrase. Deliberately crude, and it does not ask whether the specifics are
    *good* — that is the reviewer's and the scorer's job. What it catches is the
    article with none at all, which no amount of reading finds because every
    paragraph is individually fine.

    The article that produced this check ran two thousand words on a system with
    states, scores and thresholds without printing a single number.
    """
    body = article.draft.body
    prose = _without_code(body)
    words = len(prose.split())
    if words < 200:
        # Too short for a density to mean anything; a note is not an article.
        return ()
    specifics = (
        len(_NUMBER.findall(prose)) + len(_INLINE_CODE.findall(body)) + len(_QUOTED.findall(prose))
    )
    density = 1000 * specifics / words
    if density >= MIN_SPECIFICS_PER_THOUSAND:
        return ()
    return (
        ValidationFinding(
            check=ValidationCheck.CONCRETE_DETAIL,
            detail=(
                f"{specifics} concrete details in {words} words ({density:.1f} per thousand, "
                f"below {MIN_SPECIFICS_PER_THOUSAND:g}): no figure, quoted line or named "
                "value a reader could check. An article that describes a thing without "
                "showing it asks to be taken on trust"
            ),
            severity=IssueSeverity.MAJOR,
            suggested_route=FailureCategory.SUBSTANTIVE_ISSUE,
        ),
    )


def _prose_only(body: str) -> str:
    """The body with code — fenced and inline — taken out.

    Both, unlike :func:`_without_code`, which keeps inline spans because the
    number check wants them. A sentence pattern found inside `like_this` is not
    a sentence.
    """
    return _INLINE_CODE.sub(" ", _without_code(body))


def _without_code(body: str) -> str:
    """The prose with fenced code blocks removed.

    Code is not prose and must not be checked as though it were: its numbers are
    the example's, its ``#`` lines are comments rather than headings.
    """
    return re.sub(r"```.*?```", "", body, flags=re.S)


def _significant(text: str) -> frozenset[str]:
    """The words in ``text`` that would mean something if two strings shared them."""
    return frozenset(
        word
        for raw in re.findall(r"[A-Za-z][A-Za-z'-]*", text.casefold())
        if (word := raw.strip("'-")) and len(word) > 2 and word not in _STOPWORDS
    )


__all__ = [
    "LENGTH_TOLERANCE",
    "SafeCorrection",
    "ValidationCheck",
    "ValidationFinding",
    "ValidationInput",
    "run_checks",
]
