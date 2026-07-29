"""The servable application (phase 09).

``uvicorn groundscribe.api.asgi:app``.

Separate from :mod:`groundscribe.api.app`, which builds an application around
*whatever* runtime it is given, and from :mod:`groundscribe.api.openapi`, whose
application exists only to be described. This is the one wired to the local
installation, and keeping the three apart means a test never accidentally serves
production configuration and a contract export never needs a database.

A runtime is built per request, over an engine built once for the process: a
session belongs to the request that uses it, while a connection pool does not.
"""

from __future__ import annotations

from groundscribe.api.app import create_app
from groundscribe.app.bootstrap import build_runtime

app = create_app(runtime_factory=build_runtime)

__all__ = ["app"]
