"""Encryption at rest, and where the key is not (phase 13).

Spec (plan/13 → *Encryption at rest for sensitive trace content; separate secret
storage*. Test-first: *Encryption at rest* — sensitive trace content is encrypted
on disk; secrets are stored separately from trace data).

The property under test is narrow and checkable: **open the file and the article
is not in it**. Everything else — that reads still work, that dedup still dedups,
that a tampered blob is still detected — has to keep holding, because encryption
that quietly disabled the integrity check would trade a threat nobody in a
local-first tool has for one everybody has.

**Addressing stays on the plaintext.** A blob's content hash is the hash of what
it *is*, not of what it happens to look like encrypted. Two authentic encryptions
of identical bytes differ (they must — a cipher that produced identical
ciphertext for identical plaintext leaks equality), so hashing the ciphertext
would end deduplication and make every recorded content hash unverifiable.

**The key lives somewhere the trace does not.** A key beside the data it protects
is a lock taped to its own door: any backup, any copy of the blob root, any
mis-scoped share carries both. So the key store is a separate root, and the tests
check that nothing about it is written into the blob directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundscribe.privacy.encryption import (
    EncryptedBlobStore,
    KeyFileStore,
    MissingKey,
)
from groundscribe.storage.blob_store import BlobStore, content_hash

ARTICLE = b'{"title":"Read-through caching","body":"Northwind threatened to leave."}'


@pytest.fixture
def keys(tmp_path: Path) -> KeyFileStore:
    """A key store rooted somewhere other than the blobs it protects."""
    return KeyFileStore(tmp_path / "keys")


@pytest.fixture
def blob_root(tmp_path: Path) -> Path:
    return tmp_path / "blobs"


@pytest.fixture
def store(blob_root: Path, keys: KeyFileStore) -> EncryptedBlobStore:
    return EncryptedBlobStore(BlobStore(blob_root), keys)


def _files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


# ---------------------------------------------------------------------------
# On disk
# ---------------------------------------------------------------------------


def test_the_stored_bytes_are_not_the_article(store: EncryptedBlobStore, blob_root: Path) -> None:
    """The whole point, stated as bluntly as it can be tested."""
    store.put(ARTICLE)

    written = _files(blob_root)
    assert written
    for path in written:
        raw = path.read_bytes()
        assert ARTICLE not in raw
        assert b"Northwind" not in raw


def test_what_goes_in_comes_back_out(store: EncryptedBlobStore) -> None:
    """Encryption a reader cannot undo is data loss with extra steps."""
    ref = store.put(ARTICLE)

    assert store.get(ref.content_hash) == ARTICLE


def test_the_address_is_still_the_hash_of_the_article(store: EncryptedBlobStore) -> None:
    """Addressing is on the plaintext, so recorded hashes stay verifiable.

    Every provenance record in the system names an artefact by the hash of its
    content. Re-addressing on ciphertext would invalidate all of them at once,
    and silently: the rows would still parse.
    """
    ref = store.put(ARTICLE)

    assert ref.content_hash == content_hash(ARTICLE)


def test_identical_content_still_dedups(store: EncryptedBlobStore, blob_root: Path) -> None:
    """A repair that resends a near-identical request must still cost one blob."""
    first = store.put(ARTICLE)
    second = store.put(ARTICLE)

    assert first.content_hash == second.content_hash
    assert len(_files(blob_root)) == 1


def test_tampering_is_still_detected(store: EncryptedBlobStore, blob_root: Path) -> None:
    """The integrity guarantee survives.

    Encryption that disabled the hash check would trade a threat a local-first
    tool mostly does not have for one it certainly does: a blob that quietly
    stopped matching what the record says it is.
    """
    ref = store.put(ARTICLE)
    path = _files(blob_root)[0]
    path.write_bytes(path.read_bytes()[:-4] + b"junk")

    assert store.verify(ref.content_hash) is False


def test_a_missing_blob_is_still_a_missing_blob(store: EncryptedBlobStore) -> None:
    """The wrapper does not turn one failure into a different one."""
    with pytest.raises(KeyError):
        store.get(content_hash(b"never stored"))


# ---------------------------------------------------------------------------
# Where the key is not
# ---------------------------------------------------------------------------


def test_the_key_is_not_written_beside_the_data(
    store: EncryptedBlobStore, blob_root: Path, keys: KeyFileStore
) -> None:
    """A key beside the data it protects is a lock taped to its own door.

    Any backup of the blob root, any copy handed to someone for debugging, any
    mis-scoped share would otherwise carry both halves.
    """
    store.put(ARTICLE)
    key = keys.key()

    assert keys.path.parent != blob_root
    for path in _files(blob_root):
        assert key not in path.read_bytes()


def test_the_key_file_is_not_readable_by_anyone_else(keys: KeyFileStore) -> None:
    """Created 0600. A secret in a world-readable file is a published secret."""
    keys.key()

    assert keys.path.stat().st_mode & 0o077 == 0


def test_the_key_is_generated_once_and_then_reused(keys: KeyFileStore) -> None:
    """A key regenerated per process would make yesterday's traces unreadable."""
    assert keys.key() == keys.key()


def test_a_second_store_over_the_same_key_can_read_the_first(
    blob_root: Path, keys: KeyFileStore
) -> None:
    """Restarting the process is not a data-loss event."""
    first = EncryptedBlobStore(BlobStore(blob_root), keys)
    ref = first.put(ARTICLE)

    second = EncryptedBlobStore(BlobStore(blob_root), KeyFileStore(keys.path.parent))

    assert second.get(ref.content_hash) == ARTICLE


def test_a_store_with_the_wrong_key_refuses_rather_than_guesses(
    blob_root: Path, keys: KeyFileStore, tmp_path: Path
) -> None:
    """Reading with the wrong key fails loudly.

    A cipher that returned plausible-looking rubbish would put unverifiable
    content into a provenance record, which is worse than an error: the record
    would still look complete.
    """
    EncryptedBlobStore(BlobStore(blob_root), keys).put(ARTICLE)
    stranger = EncryptedBlobStore(BlobStore(blob_root), KeyFileStore(tmp_path / "other-keys"))

    with pytest.raises(MissingKey):
        stranger.get(content_hash(ARTICLE))


def test_the_key_store_reports_where_it_is_looking(keys: KeyFileStore) -> None:
    """A secret store nobody can locate is a secret store nobody can rotate."""
    assert keys.path.name
    assert str(keys.path)
