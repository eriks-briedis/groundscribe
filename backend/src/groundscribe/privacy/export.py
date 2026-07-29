"""Producing the finished article in the formats a person publishes in (phase 13).

plan/13 → *Export formats*: Markdown, plain text, HTML, clipboard-ready; export
uses the version that passed validation and matches the recorded content hash.

**What an export is of comes first.** The article is read back from the blob
store and its bytes are checked against the hash recorded for them. Rendering the
draft object a caller happened to be holding would produce a file that looks
right and is not the artefact anything was checked against — precisely the
failure a content-addressed store exists to make impossible. Tampered bytes are
refused rather than exported with a caveat: it is the one failure a reader of the
finished file could never detect for themselves.

**The rendering is conservative**, because ``body`` is the only free-text field
in the system and it is the author's prose (plan/00). Markdown is the article as
written. Plain text unwraps the markup *around* the prose without reflowing,
retitling or tidying it. HTML escapes and wraps; it deliberately does not
implement Markdown, because a half-implementation would mangle exactly the
constructs technical writing is made of — code fences, nested lists — and a
wrong-looking article is worse than a plain one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from html import escape

from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.stages.schemas import ArticleDraft
from groundscribe.storage.snapshot_store import SnapshotStore


class ExportIntegrityError(Exception):
    """The stored bytes no longer hash to what was recorded for them."""


class ExportFormat(StrEnum):
    """The four forms plan/13 names.

    ``CLIPBOARD`` is plain text with nothing around it. It is its own member
    rather than a flag on plain text because it is what a person *asks for*, and
    an option nobody can name in the UI is an option nobody uses.
    """

    MARKDOWN = "markdown"
    PLAIN_TEXT = "plain_text"
    HTML = "html"
    CLIPBOARD = "clipboard"


#: What each format is, to a browser, an editor, or an HTTP client.
MEDIA_TYPES: dict[ExportFormat, str] = {
    ExportFormat.MARKDOWN: "text/markdown",
    ExportFormat.PLAIN_TEXT: "text/plain",
    ExportFormat.HTML: "text/html",
    ExportFormat.CLIPBOARD: "text/plain",
}


@dataclass(frozen=True)
class ExportedArticle:
    """One rendered article, and the evidence of what it was rendered from.

    The version id and content hash travel with the content because an exported
    file otherwise loses its provenance at exactly the moment it leaves the
    system: a Markdown file on a desktop cannot say which run produced it.
    """

    version_id: str
    content_hash: str
    format: ExportFormat
    content: str

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.format]


def render_article(
    snapshots: SnapshotStore, version: ArtifactSnapshot, fmt: ExportFormat
) -> ExportedArticle:
    """Render the stored article version in ``fmt``, hash-checked first."""
    if not snapshots.verify(version):
        raise ExportIntegrityError(
            f"version {version.id} no longer matches the content hash recorded for it "
            f"({version.content_hash}); it cannot be exported until that is explained"
        )
    draft = ArticleDraft.model_validate_json(snapshots.read(version))
    return ExportedArticle(
        version_id=version.id,
        content_hash=version.content_hash,
        format=fmt,
        content=_RENDERERS[fmt](draft),
    )


# ----------------------------------------------------------------------
# The renderers
# ----------------------------------------------------------------------


def _markdown(draft: ArticleDraft) -> str:
    """The article as written, with the title as its top heading."""
    return f"# {draft.title}\n\n{draft.body}"


#: Markdown constructs plain text unwraps. Each removes *markup* and keeps every
#: character of prose — which is the line this exporter is not allowed to cross.
_UNWRAP: tuple[tuple[re.Pattern[str], str], ...] = (
    # A fence line goes; the code between fences stays, because in technical
    # writing the code samples are most of the article.
    (re.compile(r"^\s*(?:```|~~~).*$\n?", re.M), ""),
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""),
    (re.compile(r"^\s*>\s?", re.M), ""),
    (re.compile(r"^(\s*)[-*+]\s+", re.M), r"\1"),
    # Inline emphasis and code marks, kept narrow: only marks wrapping text on
    # one line, so a stray asterisk in prose survives.
    (re.compile(r"`([^`\n]+)`"), r"\1"),
    (re.compile(r"\*\*([^*\n]+)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)\*([^*\n]+)\*(?!\w)"), r"\1"),
    # A link becomes its text; the target is kept in brackets after it, because
    # silently dropping a URL loses something the reader was given.
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r"\1 (\2)"),
)


def _plain_text(draft: ArticleDraft) -> str:
    """The prose without the markup around it.

    Not a Markdown parser: a sequence of narrow substitutions, each of which
    removes marks and keeps every character of the author's text. The failure
    mode of a parser here is a silently reflowed article; the failure mode of
    this is a leftover asterisk.
    """
    body = draft.body
    for pattern, replacement in _UNWRAP:
        body = pattern.sub(replacement, body)
    return f"{draft.title}\n\n{body}"


def _clipboard(draft: ArticleDraft) -> str:
    """Paste-ready: the plain text and nothing around it."""
    return _plain_text(draft).strip()


def _html(draft: ArticleDraft) -> str:
    """A document a browser can open, escaped before it is wrapped.

    Escaping first is the whole rule. A technical article is exactly the document
    most likely to contain a tag as an example, and an exporter that let one
    through would be an injection in a file the author is about to publish.

    Paragraphs are the only structure inferred, from blank lines. Anything more
    would be implementing Markdown badly.
    """
    title = escape(draft.title)
    paragraphs = "\n".join(
        f"<p>{escape(block.strip())}</p>"
        for block in re.split(r"\n\s*\n", draft.body)
        if block.strip()
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"<h1>{title}</h1>\n"
        f"{paragraphs}\n"
        "</body>\n"
        "</html>\n"
    )


_RENDERERS = {
    ExportFormat.MARKDOWN: _markdown,
    ExportFormat.PLAIN_TEXT: _plain_text,
    ExportFormat.HTML: _html,
    ExportFormat.CLIPBOARD: _clipboard,
}


__all__ = [
    "MEDIA_TYPES",
    "ExportFormat",
    "ExportIntegrityError",
    "ExportedArticle",
    "render_article",
]
