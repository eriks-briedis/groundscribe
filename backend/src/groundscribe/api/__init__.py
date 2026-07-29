"""The HTTP interface (phase 09).

A translation layer over :mod:`groundscribe.app.services` and nothing else. The
generated OpenAPI schema is the contract phase 11's client is built from, which
is why the request and response shapes live in :mod:`groundscribe.api.schemas`
rather than being the storage models in disguise.
"""

from __future__ import annotations

from groundscribe.api.app import create_app

__all__ = ["create_app"]
