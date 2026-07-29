"""The ``writer`` command-line interface (phase 09).

plan/09 → *Typer CLI mirroring the spec's commands, all delegating to the service
layer*. It is a second front door onto exactly the same application service the
HTTP API calls, and it holds no editorial or workflow logic of its own — a rule
enforced by what this package is allowed to import.
"""

from __future__ import annotations

from groundscribe.cli.main import app

__all__ = ["app"]
