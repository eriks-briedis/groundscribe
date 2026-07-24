"""Content-addressed blob store tests (phase 02).

Spec (plan/02 → Test-first specification):
- **Content-addressed dedup:** storing identical content twice yields one blob
  and two references (same ``content_hash``, same ``content_location``).
- **Hash-mutation detection:** tampering with stored content is detectable
  because the recomputed hash no longer matches ``content_hash``.
- **Write-once:** a snapshotted blob is never updated in place.

This is the storage substrate under ``ArtifactSnapshot`` — content is the
identity, so the same input is stored once and referenced (plan/00 → immutability
+ content addressing).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from groundscribe.storage.blob_store import BlobStore


def test_put_returns_content_addressed_reference(tmp_path: Path) -> None:
    """The reference's hash is the sha256 of the content and drives its location."""
    store = BlobStore(tmp_path)
    content = b"the source segment text"

    ref = store.put(content)

    assert ref.content_hash == hashlib.sha256(content).hexdigest()
    assert ref.size == len(content)
    # Location is derived from the hash (a fanned-out path), not caller-chosen.
    assert ref.content_hash in ref.location.replace("/", "")
    assert store.get(ref.content_hash) == content


def test_identical_content_dedupes_to_one_blob(tmp_path: Path) -> None:
    """Two puts of identical content share one on-disk blob and one location."""
    store = BlobStore(tmp_path)
    content = b"repeated evidence passage"

    first = store.put(content)
    second = store.put(content)

    assert first.content_hash == second.content_hash
    assert first.location == second.location
    blobs = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(blobs) == 1, "identical content must be stored exactly once"


def test_distinct_content_yields_distinct_blobs(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)

    a = store.put(b"claim A")
    b = store.put(b"claim B")

    assert a.content_hash != b.content_hash
    assert a.location != b.location


def test_tampering_is_detected_by_verify(tmp_path: Path) -> None:
    """A blob mutated on disk fails integrity verification."""
    store = BlobStore(tmp_path)
    ref = store.put(b"authoritative content")
    assert store.verify(ref.content_hash) is True

    blob_path = tmp_path / ref.location
    blob_path.write_bytes(b"tampered content")

    assert store.verify(ref.content_hash) is False


def test_get_missing_hash_raises(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    with pytest.raises(KeyError):
        store.get("0" * 64)


def test_put_is_write_once_and_does_not_rewrite(tmp_path: Path) -> None:
    """Re-putting identical content leaves the existing blob byte-for-byte intact."""
    store = BlobStore(tmp_path)
    ref = store.put(b"immutable")
    blob_path = tmp_path / ref.location
    mtime_before = blob_path.stat().st_mtime_ns

    store.put(b"immutable")

    assert blob_path.stat().st_mtime_ns == mtime_before
