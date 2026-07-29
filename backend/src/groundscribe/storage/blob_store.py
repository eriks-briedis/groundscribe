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


def content_hash(content: bytes) -> str:
    """The content address of ``content``: its sha256, hex-encoded.

    Public because callers outside the store hash things that are *addressed the
    same way* without being blobs of their own — a source segment records the hash
    of its own text (phase 06). Two different hash functions for one system's
    content addresses would make those references silently incomparable.
    """
    return hashlib.sha256(content).hexdigest()


def _location_for(digest: str) -> str:
    return f"{digest[:2]}/{digest[2:]}"


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
        return self.put_at(content_hash(content), content)

    def put_at(self, digest: str, payload: bytes) -> BlobRef:
        """Store ``payload`` under an address computed by the caller.

        The one case where address and bytes come apart is phase 13's encryption
        at rest: the address must stay the hash of the *article*, while the bytes
        on disk are its ciphertext. Two authentic encryptions of identical
        plaintext differ, so addressing on what is written would end dedup and
        make every recorded content hash unverifiable.

        Ordinary callers use :meth:`put`, which computes the address from the
        content and cannot get the two out of step.
        """
        location = _location_for(digest)
        path = self._root / location
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file then atomically rename, so a crash mid-write
            # never leaves a partial blob under a valid content address.
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(payload)
            tmp.rename(path)
        return BlobRef(content_hash=digest, location=location, size=len(payload))

    def get(self, digest: str) -> bytes:
        """Return the bytes stored under ``digest``; ``KeyError`` if absent."""
        path = self._root / _location_for(digest)
        if not path.exists():
            raise KeyError(digest)
        return path.read_bytes()

    def verify(self, digest: str) -> bool:
        """True iff the stored bytes still hash to ``digest``.

        Detects accidental or malicious on-disk tampering: the recomputed hash of
        a mutated blob no longer matches the address it is stored under.
        """
        try:
            stored = self.get(digest)
        except KeyError:
            return False
        return content_hash(stored) == digest
