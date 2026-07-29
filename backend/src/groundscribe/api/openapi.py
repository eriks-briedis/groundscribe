"""Generating the API contract (phase 09).

plan/09 → *OpenAPI generation as the contract source of truth (consumed by phase
11)*, and plan/00's layout, which puts generated contracts in ``contracts/``.

The file is generated and committed, not generated at build time. Two reasons:
a reviewer sees the contract change in the same diff as the route that changed
it, and a test can assert that the committed file still matches the app — which
turns "the frontend cannot see the new endpoint" from a bug discovered in phase
11 into a failing test here.

Written with sorted keys and a trailing newline so regenerating produces a
minimal diff rather than a reshuffle. A contract whose diff is unreadable is a
contract nobody reviews.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from groundscribe.api.app import create_app
from groundscribe.app.runtime import Runtime
from groundscribe.paths import repo_root

#: Where the generated contract lives. Beside the frontend's own generated
#: types, outside the Python package, because it belongs to both sides.
CONTRACT_PATH = repo_root() / "contracts" / "openapi.json"


def contract_app() -> FastAPI:
    """An app built only to be described.

    The contract depends on the routes and their schemas, never on the runtime
    behind them, so the factory here refuses to run: exporting a contract must
    not need a database, and a factory that quietly returned something would
    hide the day someone made the schema depend on live state.
    """
    return create_app(runtime_factory=_no_runtime)


def _no_runtime() -> Runtime:
    raise RuntimeError(
        "this application exists to be described, not served; build one with a "
        "real runtime factory to handle requests"
    )


def build_schema(app: FastAPI) -> dict[str, Any]:
    """The OpenAPI document for ``app``.

    Generated fresh rather than read from ``app.openapi()``'s cache: the cached
    copy is whatever the first caller asked for, and an exporter that returned a
    memoised document could write a contract for an app that has since gained a
    route.
    """
    return get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )


def export_schema(app: FastAPI, *, path: Path = CONTRACT_PATH) -> Path:
    """Write the contract to disk, returning where it went."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(build_schema(app), indent=2, sort_keys=True)
    path.write_text(document + "\n", encoding="utf-8")
    return path


__all__ = ["CONTRACT_PATH", "build_schema", "contract_app", "export_schema"]
