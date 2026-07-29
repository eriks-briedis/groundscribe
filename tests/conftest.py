"""Shared fixtures for the cross-cutting integration tests (phase 09).

The database and storage fixtures are the backend suite's, imported rather than
rebuilt: an integration test running against its own harness would be proving
things about a second setup nobody ships.

``backend/tests`` joins ``sys.path`` so the builders written for the unit suite —
the golden data reader, the scripted-model harness — are importable from here.
They are the honest way to drive the system, and re-implementing them would give
this directory its own idea of what a golden source is.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_TESTS = Path(__file__).resolve().parents[1] / "backend" / "tests"
if str(BACKEND_TESTS) not in sys.path:
    sys.path.insert(0, str(BACKEND_TESTS))

from db_fixtures import (  # noqa: E402, F401
    blob_store,
    db_session,
    engine,
    snapshot_store,
)
