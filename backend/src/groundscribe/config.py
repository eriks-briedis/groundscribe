"""Configuration a machine holds rather than the repository (a slice of phase 13).

One secret matters so far — the shared password — and it must not be committed,
so it lives in a ``.env`` file git is told to ignore. Something has to read that
file, and this is it.

A parser rather than a dependency. What it does is a dozen lines; what it must
*not* do is the part worth writing down, and both are easier to hold to in code
that can be read in one screen than in a library's option matrix.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

from groundscribe.paths import repo_root

#: The shared password. Absent means the served application will not start.
PASSWORD_ENV = "GROUNDSCRIBE_PASSWORD"

#: Where the file lives unless a caller says otherwise.
ENV_FILE = ".env"


def load_env_file(
    path: Path | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Read ``KEY=value`` pairs into ``environ``, returning what the file held.

    The environment always wins. A deployment that exports a password and also
    happens to have a stale file next to the checkout must run with what it was
    given — a file that could silently replace real configuration would make
    "what is this process actually using?" unanswerable.

    A missing file is not an error: most installations set real environment
    variables and have no file at all. A malformed *line* is an error, because
    the alternative is a password that is quietly not set and an application
    that quietly has no lock on it.
    """
    target = environ if environ is not None else os.environ
    source = path if path is not None else repo_root() / ENV_FILE
    if not source.exists():
        return {}

    values: dict[str, str] = {}
    for number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        if "=" not in line:
            raise ValueError(f"{source}: line {number} is not KEY=value: {raw!r}")
        key, _, value = line.partition("=")
        values[key.strip()] = _unquote(value.strip())

    for key, value in values.items():
        target.setdefault(key, value)
    return values


def _unquote(value: str) -> str:
    """Drop one matching pair of surrounding quotes, if there is one.

    Quotes are how a person writes a value with spaces at either end; anything
    more elaborate — escapes, interpolation, multi-line values — is deliberately
    not supported, because a configuration file nobody can predict the meaning of
    is worse than one that refuses.
    """
    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return value[1:-1]
    return value


def configured_password(
    environ: MutableMapping[str, str] | None = None, *, env_file: Path | None = None
) -> str | None:
    """The shared password, from the environment or the ``.env`` beside the code.

    ``env_file`` is named by the tests so they answer from a file they wrote
    rather than from whatever the machine running them happens to have.
    """
    target = environ if environ is not None else os.environ
    load_env_file(env_file, environ=target)
    password = target.get(PASSWORD_ENV, "").strip()
    return password or None


__all__ = ["ENV_FILE", "PASSWORD_ENV", "configured_password", "load_env_file"]
