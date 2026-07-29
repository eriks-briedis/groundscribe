"""Encryption at rest for stored artefact content (phase 13).

plan/13 → *encryption at rest for sensitive trace content; separate secret
storage*.

Everything the pipeline records — prompts, provider responses, article versions —
ends up as bytes in the blob store. Local-first means those bytes are on a laptop
that gets backed up, synced and occasionally lent out, so "the trace is on disk in
the clear" is the default worth changing.

**Addressing stays on the plaintext.** A blob's content hash is the hash of what
it *is*. Two authentic encryptions of identical bytes differ — they must, or the
cipher would leak equality — so addressing on the ciphertext would end
deduplication and make every content hash recorded anywhere in the system
unverifiable, silently, because the rows would still parse. So the wrapper hashes
the plaintext, hands :meth:`BlobStore.put_at` that address, and writes the
ciphertext under it.

**The integrity check survives.** ``verify`` decrypts and re-hashes, so a blob
that stopped matching its record is still caught. Encryption that disabled that
check would trade a threat a local-first tool mostly does not have for one it
certainly does.

**The key lives somewhere the data does not.** A key beside the material it
protects is a lock taped to its own door: every backup of the blob root, every
copy handed over for debugging, carries both halves. :class:`KeyFileStore` keeps
it under its own root, 0600.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from groundscribe.storage.blob_store import BlobRef, BlobStore, content_hash

#: The file a key store reads and, if absent, writes.
KEY_FILENAME = "trace.key"


class MissingKey(Exception):
    """These bytes were not encrypted with the key this process holds.

    Raised rather than returning whatever the cipher produced. A record full of
    unverifiable content is worse than an error, because it still looks
    complete.
    """


class KeyFileStore:
    """The encryption key, kept apart from the data it protects.

    A file under its own root, created 0600 inside a 0700 directory, generated
    once on first use. Generated *once* because a key regenerated per process
    would make yesterday's traces unreadable — the failure would look exactly
    like corruption and arrive a day after the cause.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        #: Public so a person can find, back up, or rotate the key. A secret
        #: store nobody can locate is a secret store nobody can rotate.
        self.path = self._root / KEY_FILENAME

    def key(self) -> bytes:
        """The key from disk, generated on first use."""
        if self.path.exists():
            return self.path.read_bytes().strip()
        return self._generate()

    def _generate(self) -> bytes:
        """Write a new key, readable only by its owner."""
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = Fernet.generate_key()
        # Created with the right mode from the start rather than chmod'ed after:
        # between the two calls the key would be world-readable on disk, and
        # "briefly" is not a property a secret gets to have.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        return key


class EncryptedBlobStore:
    """A blob store whose bytes on disk are ciphertext.

    Wraps rather than replaces :class:`BlobStore`: write-once semantics, the
    atomic rename and the directory fan-out are all properties of the store
    underneath, and a second implementation of them would be a second set of
    bugs.
    """

    def __init__(self, blobs: BlobStore, keys: KeyFileStore) -> None:
        self._blobs = blobs
        self._fernet = Fernet(keys.key())

    def put(self, content: bytes) -> BlobRef:
        """Encrypt ``content`` and store it under the address of the plaintext."""
        return self._blobs.put_at(content_hash(content), self._fernet.encrypt(content))

    def get(self, digest: str) -> bytes:
        """Return the plaintext stored under ``digest``.

        ``KeyError`` if it is not there, :class:`MissingKey` if it is there and
        this process cannot read it. The two are different problems and a caller
        that could not tell them apart would go looking in the wrong place.
        """
        try:
            return self._fernet.decrypt(self._blobs.get(digest))
        except InvalidToken as error:
            raise MissingKey(
                f"blob {digest} was not encrypted with the key this process holds "
                f"(or its bytes have been altered); refusing to guess at its content"
            ) from error

    def verify(self, digest: str) -> bool:
        """True iff the stored bytes still decrypt to something hashing to ``digest``.

        One check covering two failures — a tampered blob and a swapped one —
        because from the record's point of view they are the same failure: the
        content is not what the record says it is.
        """
        try:
            return content_hash(self.get(digest)) == digest
        except (KeyError, MissingKey):
            return False


__all__ = [
    "KEY_FILENAME",
    "EncryptedBlobStore",
    "KeyFileStore",
    "MissingKey",
]
