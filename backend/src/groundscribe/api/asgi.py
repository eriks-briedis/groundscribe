"""The servable application (phase 09; locked in a slice of phase 13).

``uvicorn --factory groundscribe.api.asgi:served_app``.

Separate from :mod:`groundscribe.api.app`, which builds an application around
*whatever* runtime it is given, and from :mod:`groundscribe.api.openapi`, whose
application exists only to be described. This is the one wired to the local
installation, and keeping the three apart means a test never accidentally serves
production configuration and a contract export never needs a database.

A runtime is built per request, over an engine built once for the process: a
session belongs to the request that uses it, while a connection pool does not.

**It refuses to start without a password.** ``create_app`` defaults to open
because the test suite builds hundreds of applications to say nothing about
security; serving one is a different act, and this is where that difference is
enforced. A factory rather than a module-level instance so that importing this
module — which the tests do — is not itself an attempt to serve.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path

from fastapi import FastAPI

from groundscribe.api.app import create_app
from groundscribe.app.bootstrap import build_runtime
from groundscribe.config import ENV_FILE, PASSWORD_ENV, configured_password
from groundscribe.observability.logging import configure_logging


def served_app(
    environ: MutableMapping[str, str] | None = None, *, env_file: Path | None = None
) -> FastAPI:
    """The local installation, with a lock on it."""
    # Here rather than at import: a served process wants structured output, and a
    # test importing this module to check the refusal above does not.
    configure_logging()
    password = configured_password(environ, env_file=env_file)
    if password is None:
        raise RuntimeError(
            f"{PASSWORD_ENV} is not set, so this would serve the whole pipeline to "
            f"anything that can reach the port. Put it in {ENV_FILE} (scripts/dev.sh "
            f"writes one for you) or export it."
        )
    return create_app(
        runtime_factory=build_runtime,
        # The read side gets a runtime that will not take a write lock, which on
        # the default SQLite installation is what keeps every screen answering
        # while a stage is running (KNOWN-ISSUES §1).
        reader_factory=lambda: build_runtime(reading=True),
        password=password,
    )


__all__ = ["served_app"]
