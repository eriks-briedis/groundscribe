"""Filesystem-backed content-addressed blob store (phase 02).

Content is the identity: a blob's address is the sha256 of its bytes, so the same
input is stored exactly once and merely referenced again (plan/00 → immutability
+ content addressing). This is the substrate an :class:`ArtifactSnapshot`
references by ``content_hash`` / ``content_location``.

Filesystem-backed for local dev; the interface is deliberately small so a
Postgres/large-object or object-store backend can replace it without touching
callers. The hash is fanned out into a two-character subdirectory to avoid one
directory holding every blob.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BlobRef:
    """A content-addressed reference to stored bytes.

    Frozen: a reference, once handed out, describes an immutable blob. The
    ``location`` is relative to the store root so references stay portable if the
    root moves.
    """

    content_hash: str
    location: str
    size: int


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _location_for(content_hash: str) -> str:
    return f"{content_hash[:2]}/{content_hash[2:]}"


class BlobStore:
    """Write-once, deduplicating, content-addressed store over a directory."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> BlobRef:
        """Store ``content`` and return its reference.

        Write-once and idempotent: if a blob with the same hash already exists it
        is left byte-for-byte untouched (identical content is, by construction,
        already there), so repeated puts dedup to a single blob.
        """
        content_hash = _hash(content)
        location = _location_for(content_hash)
        path = self._root / location
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file then atomically rename, so a crash mid-write
            # never leaves a partial blob under a valid content address.
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(content)
            tmp.rename(path)
        return BlobRef(content_hash=content_hash, location=location, size=len(content))

    def get(self, content_hash: str) -> bytes:
        """Return the bytes stored under ``content_hash``; ``KeyError`` if absent."""
        path = self._root / _location_for(content_hash)
        if not path.exists():
            raise KeyError(content_hash)
        return path.read_bytes()

    def exists(self, content_hash: str) -> bool:
        return (self._root / _location_for(content_hash)).exists()

    def verify(self, content_hash: str) -> bool:
        """True iff the stored bytes still hash to ``content_hash``.

        Detects accidental or malicious on-disk tampering: the recomputed hash of
        a mutated blob no longer matches the address it is stored under.
        """
        try:
            stored = self.get(content_hash)
        except KeyError:
            return False
        return _hash(stored) == content_hash
