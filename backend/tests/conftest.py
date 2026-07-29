"""Shared pytest fixtures for the groundscribe backend test suite.

The database harness itself lives in :mod:`db_fixtures`, which the cross-cutting
suite under ``tests/`` imports too: two directories cannot both contribute a
plugin called ``conftest``, and duplicating the harness would give the
integration tests their own, quietly different, idea of what isolation means.
"""

from __future__ import annotations

from db_fixtures import (  # noqa: F401
    blob_store,
    db_session,
    engine,
    snapshot_store,
)
