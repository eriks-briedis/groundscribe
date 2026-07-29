"""Where secrets come from, and where they are kept (phase 13).

Spec (plan/13 → *Secret management*: env vars in dev, OS keychain for packaged
desktop, encrypted storage for hosted; keys never written to logs, prompts,
artefacts or traces).

Three deployment stories, one interface. A keychain and a hosted secret manager
both end with a process holding a value, and an environment variable is the one
handover every launcher can perform — a shell profile, a systemd unit, a
``launchd`` plist reading from the Keychain, a container secret mounted as an
env var. So the key store reads the environment first and falls back to its file.

Reading the environment *first* is the load-bearing order. A deployment that
supplies a key and also has a stale file next to the checkout must run with what
it was given; a file that could silently win would make "which key is this
process actually using?" unanswerable, and the symptom would be traces that
cannot be read back.

Encryption also has to be something a deployment turns on rather than something
it discovers. The default is off, because switching it on for an existing
installation without a migration would make every blob already on disk
unreadable — the sort of upgrade that presents as total data loss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundscribe.app.bootstrap import TRACE_ENCRYPTION_ENV, blob_storage
from groundscribe.privacy.encryption import (
    TRACE_KEY_ENV,
    EncryptedBlobStore,
    KeyFileStore,
)
from groundscribe.storage.blob_store import BlobStore


def test_a_supplied_key_is_used_as_it_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment is where a keychain or a secret manager hands over."""
    supplied = KeyFileStore(tmp_path / "generated").key()
    monkeypatch.setenv(TRACE_KEY_ENV, supplied.decode("utf-8"))

    assert KeyFileStore(tmp_path / "unused").key() == supplied


def test_a_supplied_key_is_not_written_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key handed over by a secret manager stays the secret manager's.

    Copying it into a file would create a second, unmanaged copy that outlives
    rotation and that nobody knows to delete.
    """
    supplied = KeyFileStore(tmp_path / "generated").key()
    monkeypatch.setenv(TRACE_KEY_ENV, supplied.decode("utf-8"))
    root = tmp_path / "never-written"

    KeyFileStore(root).key()

    assert not root.exists()


def test_the_environment_wins_over_a_file_that_is_already_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise "which key is this process using?" has no answer.

    The failure a stale file causes is not an error message; it is a trace that
    silently cannot be read back.
    """
    store = KeyFileStore(tmp_path / "keys")
    on_disk = store.key()
    supplied = KeyFileStore(tmp_path / "other").key()
    monkeypatch.setenv(TRACE_KEY_ENV, supplied.decode("utf-8"))

    assert on_disk != supplied
    assert store.key() == supplied


# ---------------------------------------------------------------------------
# Turning it on
# ---------------------------------------------------------------------------


def test_encryption_is_off_unless_a_deployment_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silently switching it on would make every existing blob unreadable.

    An upgrade that presents as total data loss is worse than a default that has
    to be chosen, so this one is chosen.
    """
    monkeypatch.delenv(TRACE_ENCRYPTION_ENV, raising=False)

    assert isinstance(blob_storage(tmp_path / "blobs"), BlobStore)


def test_asking_for_it_gets_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TRACE_ENCRYPTION_ENV, "1")

    assert isinstance(blob_storage(tmp_path / "blobs"), EncryptedBlobStore)


def test_the_key_root_is_not_under_the_blob_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The separation the whole scheme rests on, asserted where it is decided."""
    monkeypatch.setenv(TRACE_ENCRYPTION_ENV, "yes")
    blobs = tmp_path / "var" / "blobs"

    blob_storage(blobs)

    written = [path for path in (tmp_path / "var").rglob("*") if path.is_file()]
    assert written, "a key should have been generated"
    assert all(not path.is_relative_to(blobs) for path in written)
