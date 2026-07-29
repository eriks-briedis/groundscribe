"""Producing the finished article in the formats a person publishes in (phase 13).

Spec (plan/13 → *Export formats*: Markdown, plain text, HTML, clipboard-ready;
export uses the version that passed validation and matches the recorded content
hash. Test-first: *Export fidelity*).

The formats are the easy half. The half worth testing is what an export is
allowed to be *of*: the snapshot that passed validation, read back from the blob
store and checked against its recorded hash. Rendering the draft object a caller
happened to be holding would produce a file that looks right and is not the
artefact anything was checked against — which is precisely the failure the
content-addressed store exists to make impossible.

So an export names the version, the hash it verified, and the format; and it
refuses rather than guesses when the bytes on disk no longer hash to what was
recorded.

The rendering rules are conservative for one reason: ``body`` is the only
free-text field in the system and it is the author's prose. Plain text unwraps
the Markdown *around* the prose; it does not reflow, retitle or tidy it. HTML
escapes and wraps; it does not implement Markdown, because a half-implementation
would silently mangle exactly the constructs — code fences, nested lists — that
technical writing is made of.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.privacy.export import (
    ExportedArticle,
    ExportFormat,
    ExportIntegrityError,
    render_article,
)
from groundscribe.stages.schemas import ArticleDraft
from groundscribe.storage.blob_store import content_hash
from groundscribe.storage.snapshot_store import SnapshotStore

ARTICLE = ArticleDraft(
    title="Read-through caching for the render pipeline",
    thesis="A read-through cache cut p99 render latency.",
    body=(
        "## What we shipped\n"
        "\n"
        "p99 latency fell from 810ms to 120ms. The cache is keyed by path.\n"
        "\n"
        "```python\n"
        "def key(request):\n"
        "    return request.path\n"
        "```\n"
        "\n"
        "- keys ignored the locale\n"
        "- the CDN cached the error page\n"
    ),
)


def _stored(snapshot_store: SnapshotStore, draft: ArticleDraft = ARTICLE) -> ArtifactSnapshot:
    """The article as the pipeline stores it: a content-addressed snapshot."""
    return snapshot_store.write(
        artifact_type=ArtifactType.ARTICLE_VERSION,
        content=draft.model_dump_json().encode("utf-8"),
        created_by_execution_id="exec-1",
    )


# ---------------------------------------------------------------------------
# What an export is of
# ---------------------------------------------------------------------------


def test_the_export_is_read_from_the_stored_snapshot(snapshot_store: SnapshotStore) -> None:
    """Not from an object the caller was holding.

    The point of a content-addressed store is that "the article that passed
    validation" is a fact about bytes on disk, not about whichever draft happened
    to be in memory when someone pressed export.
    """
    snapshot = _stored(snapshot_store)

    exported = render_article(snapshot_store, snapshot, ExportFormat.MARKDOWN)

    assert isinstance(exported, ExportedArticle)
    assert exported.version_id == snapshot.id
    assert exported.content_hash == snapshot.content_hash
    assert ARTICLE.title in exported.content


def test_the_hash_is_verified_before_anything_is_rendered(
    snapshot_store: SnapshotStore, tmp_path: Path
) -> None:
    """Tampered bytes are refused, not exported with a warning.

    This is the one failure a reader of the finished file could never detect for
    themselves, so it is the one the exporter must not pass along.
    """
    snapshot = _stored(snapshot_store)
    (tmp_path / snapshot.content_location).write_bytes(
        b'{"title":"tampered","thesis":"t","body":"b"}'
    )

    with pytest.raises(ExportIntegrityError):
        render_article(snapshot_store, snapshot, ExportFormat.MARKDOWN)


def test_the_export_records_which_format_it_is() -> None:
    """A file with no format is a file whose reader has to guess."""
    assert {fmt.value for fmt in ExportFormat} == {
        "markdown",
        "plain_text",
        "html",
        "clipboard",
    }


# ---------------------------------------------------------------------------
# The formats
# ---------------------------------------------------------------------------


def test_markdown_is_the_article_as_written(snapshot_store: SnapshotStore) -> None:
    """The title as a heading, then the body verbatim.

    Verbatim matters: the body is the only free-text field in the system, it is
    the author's prose, and an exporter that reflowed it would be editing at the
    last possible moment.
    """
    exported = render_article(snapshot_store, _stored(snapshot_store), ExportFormat.MARKDOWN)

    assert exported.content.startswith(f"# {ARTICLE.title}\n")
    assert ARTICLE.body in exported.content
    assert exported.media_type == "text/markdown"


def test_plain_text_removes_the_markup_and_keeps_the_prose(
    snapshot_store: SnapshotStore,
) -> None:
    """Heading marks, fences and bullets go; the sentences are untouched."""
    exported = render_article(snapshot_store, _stored(snapshot_store), ExportFormat.PLAIN_TEXT)

    assert "##" not in exported.content
    assert "```" not in exported.content
    assert "What we shipped" in exported.content
    assert "p99 latency fell from 810ms to 120ms." in exported.content
    assert "keys ignored the locale" in exported.content
    assert exported.media_type == "text/plain"


def test_plain_text_keeps_the_code_a_reader_needs(snapshot_store: SnapshotStore) -> None:
    """The fence goes; the code inside it does not.

    Technical writing is mostly the code samples. An exporter that dropped a
    fenced block along with its fence would remove the part of the article that
    was hardest to get right.
    """
    exported = render_article(snapshot_store, _stored(snapshot_store), ExportFormat.PLAIN_TEXT)

    assert "def key(request):" in exported.content
    assert "return request.path" in exported.content


def test_html_escapes_before_it_wraps(snapshot_store: SnapshotStore) -> None:
    """An article about HTML must not become HTML.

    Escaping first is the whole rule. A technical article is exactly the document
    most likely to contain a tag as an example, and an exporter that let one
    through would be an injection in a file the author is about to publish.
    """
    draft = ARTICLE.model_copy(
        update={"body": "Render `<script>alert(1)</script>` and see.", "unresolved": ()}
    )
    exported = render_article(snapshot_store, _stored(snapshot_store, draft), ExportFormat.HTML)

    assert "<script>" not in exported.content
    assert "&lt;script&gt;" in exported.content
    assert exported.media_type == "text/html"


def test_html_carries_the_title_as_a_document(snapshot_store: SnapshotStore) -> None:
    """Something a browser can open, not a fragment."""
    exported = render_article(snapshot_store, _stored(snapshot_store), ExportFormat.HTML)

    assert exported.content.startswith("<!doctype html>")
    assert f"<title>{ARTICLE.title}</title>" in exported.content


def test_clipboard_is_the_plain_text_with_nothing_around_it(
    snapshot_store: SnapshotStore,
) -> None:
    """Clipboard-ready means paste-ready: no front matter, no trailing report."""
    clipboard = render_article(snapshot_store, _stored(snapshot_store), ExportFormat.CLIPBOARD)
    plain = render_article(snapshot_store, _stored(snapshot_store), ExportFormat.PLAIN_TEXT)

    assert clipboard.content == plain.content.strip()
    assert clipboard.media_type == "text/plain"


# ---------------------------------------------------------------------------
# Fidelity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", list(ExportFormat))
def test_every_format_preserves_the_sentences_of_the_article(
    snapshot_store: SnapshotStore, fmt: ExportFormat
) -> None:
    """plan/13 → export fidelity: the passed version's content, in every format.

    Asserted on a sentence rather than on bytes, because the formats differ by
    definition. What may not differ is what the article says.
    """
    exported = render_article(snapshot_store, _stored(snapshot_store), fmt)

    assert "p99 latency fell from 810ms to 120ms." in exported.content
    assert exported.content_hash == content_hash(ARTICLE.model_dump_json().encode("utf-8"))
