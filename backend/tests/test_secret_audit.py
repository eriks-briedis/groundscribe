"""Nothing secret survives anywhere derived, swept table by table (phase 13).

Spec (plan/13 → *keys never written to logs/prompts/artefacts/traces — redaction
before persistence, from phase 03, extended and **audited** here*. Test-first:
*Redaction end-to-end* — an injected secret is absent from every persisted record
and every export, while the record still exists).

Phase 03 tested the redactor, and tested that the recorder calls it. This is a
different kind of test, and phase 13 is where it belongs: it runs a real pipeline
over source material with a key, an unrecognisable passphrase and a confidential
passage pasted into it, then **sweeps every column of every table and every blob
on disk** looking for them.

The sweep is the point. A unit test proves the rule where it is applied; only an
audit proves there is nowhere it is not. The failure it exists to catch is a
column added next year by someone who did not know the rule existed — no test
they write will fail, and this one will.

**The author's own source is exempt, and named.** ``source_documents`` and
``source_segments`` hold what a person pasted, on their own machine. Scrubbing
those rows would delete the material they are writing about and leave them unable
to see or fix the paste — and it would silently break every citation, because a
segment's character offsets index the document as it arrived. Redaction is a rule
about what crosses a boundary: what is sent to a provider, kept as a trace, or
exported. The exemption is asserted as *exactly those two tables*, so it stays a
decision rather than becoming a hole.

The invariant is two-sided throughout, as phase 03 stated it: the secret must be
gone **and** the record must still be there. Redaction that dropped the payload
would pass every hunt in this file and destroy the provenance the product exists
to provide.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from golden import with_segment_ids
from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import SourceFormat
from groundscribe.privacy.traces import export_traces
from groundscribe.provenance import models
from groundscribe.provenance.redaction import Redactor
from groundscribe.stages.base import StageRunner
from groundscribe.stages.extraction import EXTRACTION_STAGE, ExtractSourceTruth
from groundscribe.stages.ingestion import IngestSource
from groundscribe.storage.snapshot_store import SnapshotStore
from stage_helpers import DEFAULT_CONSTRAINTS, scripted_context

#: A credential shaped like one, so the pattern rules have something to find.
API_KEY = "sk-live-3Qk9Zx7Tn2Vb8Lm4Rp6Yw1Hs"

#: A credential shaped like nothing at all, registered with the redactor. This is
#: the one that matters: no pattern could find it, and a system that caught only
#: recognisable secrets would be a system that caught the easy half.
PASSPHRASE = "gently down the stream"

CONFIDENTIAL = "Northwind threatened to terminate the contract."

#: The two tables holding the author's own material rather than a derived record.
AUTHORS_OWN = frozenset({"source_documents", "source_segments"})

SOURCE = f"""\
We shipped a read-through cache in March.

The deploy used api_key={API_KEY} and the operator passphrase was
{PASSPHRASE}, which should never have been written down here.

[[CONFIDENTIAL]]
{CONFIDENTIAL}
[[/CONFIDENTIAL]]

p99 latency fell from 810ms to 120ms.
"""

SCRIPTED_MODEL: dict[str, Any] = {
    "schema_version": 1,
    "summary": "A read-through cache cut p99 render latency.",
    "claims": [
        {
            "id": "c1",
            "text": "p99 latency fell from 810ms to 120ms.",
            "classification": "directly_supported_fact",
            "evidence": [{"segment_ids": ["S3"], "quote": "p99 latency fell"}],
            "qualification_required": False,
        }
    ],
}

#: Everything the audit hunts for.
FORBIDDEN = (API_KEY, PASSPHRASE, CONFIDENTIAL)


@pytest.fixture
async def leaked(db_session: Session, snapshot_store: SnapshotStore) -> Session:
    """A run whose source had a key, a passphrase and a secret pasted into it."""
    context, client = scripted_context(
        db_session, snapshot_store, redactor=Redactor(secrets=[PASSPHRASE])
    )
    ingested = (
        await StageRunner(context).run(
            IngestSource(
                title="Cache postmortem",
                text=SOURCE,
                source_format=SourceFormat.MARKDOWN,
                constraints=DEFAULT_CONSTRAINTS,
            )
        )
    ).value
    client.script_response(EXTRACTION_STAGE, with_segment_ids(SCRIPTED_MODEL, ingested))
    await StageRunner(context).run(ExtractSourceTruth(source=ingested))
    db_session.flush()
    return db_session


def _stored_values(session: Session) -> list[tuple[str, str, str]]:
    """Every text-ish value in every table, as ``(table, column, value)``."""
    inspector = inspect(session.get_bind())
    found: list[tuple[str, str, str]] = []
    for table in inspector.get_table_names():
        columns = [column["name"] for column in inspector.get_columns(table)]
        if not columns:
            continue
        selected = ", ".join(f'"{column}"' for column in columns)
        for row in session.execute(text(f'SELECT {selected} FROM "{table}"')):
            for column, value in zip(columns, row, strict=True):
                if isinstance(value, str):
                    found.append((table, column, value))
                elif isinstance(value, dict | list):
                    found.append((table, column, json.dumps(value)))
    return found


def _offenders(session: Session) -> list[tuple[str, str, str]]:
    """Every place a forbidden string was found, with the table and column."""
    return [
        (table, column, secret)
        for table, column, value in _stored_values(session)
        for secret in FORBIDDEN
        if secret in value
    ]


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_derived_record_holds_any_of_them(leaked: Session) -> None:
    """Every column of every table, not the ones anyone remembered to check.

    The failure this exists for is a column added next year by someone who did
    not know the rule: no test they write will fail, and this one will.
    """
    escaped = [found for found in _offenders(leaked) if found[0] not in AUTHORS_OWN]

    assert escaped == []


@pytest.mark.asyncio
async def test_the_only_exemption_is_the_author_s_own_source(leaked: Session) -> None:
    """The exemption is a decision, so it is asserted rather than assumed.

    If redaction ever stopped covering some other table, the test above would
    fail; if the exemption ever quietly widened, this one would.
    """
    exempt = {table for table, _, _ in _offenders(leaked)}

    assert exempt <= AUTHORS_OWN


@pytest.mark.asyncio
async def test_no_blob_on_disk_holds_any_of_them(leaked: Session, tmp_path: Path) -> None:
    """The other half of the store: rows are not where most payload text lives.

    Including the source document's own snapshot, which *is* redacted even though
    the rows beside it are not — it is a stored artefact, content-addressed,
    shared and exportable, and it crosses the boundary the rows do not.
    """
    escaped = [
        (path.name, secret)
        for path in tmp_path.rglob("*")
        if path.is_file()
        for secret in FORBIDDEN
        if secret.encode("utf-8") in path.read_bytes()
    ]

    assert escaped == []


@pytest.mark.asyncio
async def test_no_export_carries_any_of_them(
    leaked: Session, snapshot_store: SnapshotStore
) -> None:
    """An export is the moment a trace leaves the machine.

    Full, not sanitised: sanitising removes the payloads wholesale and would
    prove nothing about whether redaction happened before they were written.
    """
    exported = export_traces(leaked, snapshot_store, "p1", confidential_material_acknowledged=True)
    body = exported.to_json()

    assert [secret for secret in FORBIDDEN if secret in body] == []


# ---------------------------------------------------------------------------
# The other half of the invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_records_are_all_still_there(leaked: Session) -> None:
    """Redaction that dropped the payload would pass every hunt above.

    It would also destroy the provenance the product exists to provide, which is
    why the invariant has always been two-sided.
    """
    invocations = leaked.scalars(select(models.ModelInvocation)).all()

    assert invocations
    assert all(invocation.request_snapshot is not None for invocation in invocations)
    assert leaked.scalars(select(models.StageExecution)).all()
    assert leaked.scalars(select(models.TraceEvent)).all()


@pytest.mark.asyncio
async def test_the_prompt_is_still_a_usable_prompt(
    leaked: Session, snapshot_store: SnapshotStore
) -> None:
    """What is left has to be worth keeping.

    A stored request scrubbed down to placeholders would satisfy the letter of
    redaction and be useless for the replay it exists to support.
    """
    invocation = leaked.scalars(select(models.ModelInvocation)).first()
    assert invocation is not None
    assert invocation.request_snapshot is not None

    stored = snapshot_store.read(invocation.request_snapshot).decode("utf-8")

    assert "read-through cache" in stored
    assert "REDACTED" in stored


@pytest.mark.asyncio
async def test_the_author_can_still_see_what_they_pasted(leaked: Session) -> None:
    """The exemption, from the other side.

    Redaction is a rule about crossing a boundary. The source rows are the
    author's material on the author's machine; scrubbing them would delete the
    thing they were writing about and leave them unable to fix the paste.
    """
    segments = leaked.scalars(select(domain_models.SourceSegment)).all()

    assert any(CONFIDENTIAL in segment.text for segment in segments)


@pytest.mark.asyncio
async def test_the_sweep_actually_reaches_the_store(leaked: Session) -> None:
    """A guard on the audit itself.

    Every assertion above is of the form "nothing was found". A sweep that
    silently read no tables, or a fixture that quietly recorded nothing, would
    satisfy all of them and prove the opposite of what they claim. So: the sweep
    sees a substantial store, and it does find the strings — in the one place
    they are allowed to be.
    """
    values = _stored_values(leaked)

    assert len(values) > 100
    assert {table for table, _, _ in values} > {"model_invocations", "trace_events"}
    assert _offenders(leaked)
