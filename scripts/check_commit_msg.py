#!/usr/bin/env python
"""Enforce conventional-commit prefixes on commit messages.

Wired as a ``commit-msg`` pre-commit hook (see ``.pre-commit-config.yaml``). The
subject line must start with one of the allowed types, an optional
``(scope)``, an optional ``!`` breaking-change marker, then ``: `` and a
description. This mirrors the commit discipline documented in CONTRIBUTING.md so
the codebase's own provenance stays legible.
"""

from __future__ import annotations

import re
import sys

ALLOWED_TYPES = (
    "test",
    "feat",
    "fix",
    "refactor",
    "chore",
    "docs",
    "migrate",
    "perf",
    "ci",
    "build",
    "style",
    "revert",
)

_SUBJECT_RE = re.compile(
    r"^(?P<type>" + "|".join(ALLOWED_TYPES) + r")"
    r"(?:\([\w.-]+\))?!?: .+"
)


def check(message: str) -> str | None:
    """Return an error string if the first non-comment line is invalid, else None."""
    for line in message.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        subject = stripped
        break
    else:
        return "empty commit message"

    # Merge commits are exempt.
    if subject.startswith("Merge "):
        return None
    if _SUBJECT_RE.match(subject):
        return None
    return (
        f"invalid commit subject: {subject!r}\n"
        f"expected '<type>(<scope>): <description>' with type in {ALLOWED_TYPES}"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_commit_msg.py <commit-msg-file>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        message = handle.read()
    error = check(message)
    if error is not None:
        print(f"commit-msg: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
