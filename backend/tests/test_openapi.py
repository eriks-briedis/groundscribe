"""The OpenAPI contract (phase 09).

Spec (plan/09 → Test-first specification): *the schema generates and includes all
command endpoints + response models*, and (Deliverables) *OpenAPI generation as
the contract source of truth*, consumed by phase 11.

"Source of truth" is what these tests are really about. Phase 11 generates its
client from the written file, so a route added without a regenerated contract is
a feature the frontend cannot see, and a contract regenerated from a broken app
is worse. The endpoint list is therefore pinned *from the plan*, not read back
out of the app — a test that asked the app what it implements would agree with
itself no matter what was missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from service_helpers import build_harness
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.api.openapi import CONTRACT_PATH, build_schema, export_schema
from groundscribe.storage.snapshot_store import SnapshotStore

#: Every command endpoint plan/09 names, as ``(method, path)``. Written out by
#: hand from the plan so the list is a statement of intent rather than a mirror.
REQUIRED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("post", "/projects"),
    ("post", "/projects/{project_id}/sources"),
    ("post", "/projects/{project_id}/source-model/extract"),
    ("post", "/projects/{project_id}/source-gaps/{gap_id}/answer"),
    ("post", "/projects/{project_id}/architecture/propose"),
    ("put", "/projects/{project_id}/architecture/{version}"),
    ("post", "/projects/{project_id}/architecture/{version}/approve"),
    ("post", "/articles/{article_id}/brief/generate"),
    ("post", "/articles/{article_id}/draft"),
    ("post", "/articles/{article_id}/review"),
    ("post", "/articles/{article_id}/revision-plan"),
    ("post", "/articles/{article_id}/rewrite"),
    ("post", "/articles/{article_id}/voice-align"),
    ("post", "/articles/{article_id}/score"),
    ("post", "/articles/{article_id}/validate"),
    ("post", "/articles/{article_id}/approve"),
    ("post", "/executions/{execution_id}/replay"),
    ("post", "/executions/{execution_id}/fork"),
    ("get", "/executions/{execution_id}"),
    ("get", "/executions/{execution_id}/events"),
    ("get", "/executions/{execution_id}/invocations"),
    ("get", "/executions/compare"),
    ("post", "/experiments"),
    ("get", "/jobs/{job_id}/events"),
)


def schema(db_session: Session, snapshot_store: SnapshotStore) -> dict[str, Any]:
    harness = build_harness(db_session, snapshot_store)
    return build_schema(create_app(runtime_factory=lambda: harness.runtime))


def test_every_endpoint_the_plan_names_is_in_the_schema(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/09 → *includes all command endpoints*."""
    paths = schema(db_session, snapshot_store)["paths"]

    missing = [
        f"{method.upper()} {path}"
        for method, path in REQUIRED_ENDPOINTS
        if method not in paths.get(path, {})
    ]

    assert missing == []


def test_every_command_advertises_the_response_a_client_must_handle(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/09 → *+ response models*.

    A generated client is only useful if the contract names the shapes. The
    command envelope in particular has to be there, because every command
    returns it and a client that had to guess would guess per endpoint.
    """
    document = schema(db_session, snapshot_store)
    components = document["components"]["schemas"]

    assert "CommandResponse" in components
    assert set(components["CommandResponse"]["properties"]) >= {
        "project_id",
        "state",
        "available_actions",
        "job",
    }
    created = document["paths"]["/projects"]["post"]["responses"]["201"]
    assert "CommandResponse" in json.dumps(created)


def test_the_schema_names_the_product_and_its_version(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A contract nobody can identify is a contract nobody can pin."""
    info = schema(db_session, snapshot_store)["info"]

    assert info["title"] == "groundscribe"
    assert info["version"]


def test_exporting_writes_the_contract_where_the_frontend_reads_it(
    tmp_path: Path, db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/00's layout puts generated contracts in ``contracts/``."""
    harness = build_harness(db_session, snapshot_store)
    destination = tmp_path / "openapi.json"

    written = export_schema(create_app(runtime_factory=lambda: harness.runtime), path=destination)

    assert written == destination
    assert json.loads(destination.read_text(encoding="utf-8"))["info"]["title"] == "groundscribe"
    assert destination.read_text(encoding="utf-8").endswith("\n")


def test_the_checked_in_contract_matches_the_app(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The committed file is the contract, so it must not drift from the code.

    Phase 11 generates its client from what is on disk. A route added without
    regenerating leaves the frontend unable to see it, and the failure would
    otherwise surface a phase later as a missing method rather than here as a
    stale file.
    """
    assert CONTRACT_PATH.exists(), f"{CONTRACT_PATH} has not been generated"

    stored = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert stored == schema(db_session, snapshot_store)
