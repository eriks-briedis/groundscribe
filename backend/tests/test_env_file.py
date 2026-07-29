"""Reading configuration out of a ``.env`` file (a slice of phase 13).

The password lives outside the repository, in a file git is told to ignore. That
file has to be read by something, and the something is deliberately small: a
parser for ``KEY=value`` lines rather than a dependency, because what it must do
is short and what it must *not* do is the interesting part.

It must not override a variable that is already set — an environment is what a
deployment says, and a checked-out file is what a machine happens to have lying
next to it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundscribe.config import load_env_file


def test_it_reads_the_pairs_a_person_would_write(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# the shared password",
                "GROUNDSCRIBE_PASSWORD=correct horse battery staple",
                "",
                "  GROUNDSCRIBE_BLOB_ROOT = /srv/blobs  ",
                "QUOTED='single'",
                'ALSO_QUOTED="double"',
                "export EXPORTED=yes",
            ]
        ),
        encoding="utf-8",
    )

    values = load_env_file(path, environ={})

    assert values == {
        "GROUNDSCRIBE_PASSWORD": "correct horse battery staple",
        "GROUNDSCRIBE_BLOB_ROOT": "/srv/blobs",
        "QUOTED": "single",
        "ALSO_QUOTED": "double",
        "EXPORTED": "yes",
    }


def test_it_leaves_alone_what_the_environment_already_says(tmp_path: Path) -> None:
    """The environment wins, always.

    A deployment that exports a password and also happens to have a stale ``.env``
    on disk must run with the password it was given, not the one it found.
    """
    path = tmp_path / ".env"
    path.write_text("GROUNDSCRIBE_PASSWORD=from-the-file\n", encoding="utf-8")
    environ = {"GROUNDSCRIBE_PASSWORD": "from-the-environment"}

    load_env_file(path, environ=environ)

    assert environ["GROUNDSCRIBE_PASSWORD"] == "from-the-environment"


def test_it_writes_what_it_read_into_the_environment(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("GROUNDSCRIBE_PASSWORD=from-the-file\n", encoding="utf-8")
    environ: dict[str, str] = {}

    load_env_file(path, environ=environ)

    assert environ["GROUNDSCRIBE_PASSWORD"] == "from-the-file"


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """Most installations set real environment variables and have no file at all."""
    assert load_env_file(tmp_path / "nothing-here", environ={}) == {}


def test_a_line_that_is_not_a_pair_is_refused(tmp_path: Path) -> None:
    """Loudly, because the alternative is a password that silently is not set."""
    path = tmp_path / ".env"
    path.write_text("GROUNDSCRIBE_PASSWORD\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1"):
        load_env_file(path, environ={})
