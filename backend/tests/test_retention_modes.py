"""Trace-retention modes (phase 13).

Spec (plan/13 → *Trace-retention modes*: full / redacted-full /
metadata-and-structured-only / no-raw-provider-payloads /
temporary-raw-retention / minimal-operational-logging; local-first may default to
detailed retention but the choice is explicit. Test-first: *Retention-mode
filtering* — each mode persists exactly the permitted record classes).

A mode says which **payload classes** survive to disk. The rows themselves — the
invocation, its provider, model, outcome, timings and cost — are the record, and
they are kept under every mode including the most restrictive. A trace that
forgot a call happened would not be a smaller trace; it would be a wrong one.

Two of the six need a note, because they are the two that could be misread:

- **full** is not "unredacted". Redaction before persistence is a product
  principle (plan/00), not a retention setting, so nothing here can switch it
  off. What ``full`` means is that every payload class is kept, indefinitely.
- **redacted-full** is the one that goes further than the floor: it additionally
  removes the project's own restricted source material from stored payloads. That
  is a real difference from ``full`` and it is what a person actually wants when
  they ask for it — keep the whole trace, minus the sensitive source.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from groundscribe.privacy.retention import (
    RetentionMode,
    RetentionPolicy,
    expire_raw_payloads,
)
from groundscribe.provenance import models
from groundscribe.provenance.enums import InvocationOutcome
from groundscribe.provenance.schemas import EffectiveRequest
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import make_recorder, seed_project

PROMPT = "Summarise the postmortem for a senior audience."

REQUEST = EffectiveRequest(
    template_id="extract_source_truth",
    template_version="1",
    rendered_prompt=PROMPT,
)

RAW = '{"summary":"a read-through cache cut p99 latency"}'

PARSED = {"summary": "a read-through cache cut p99 latency"}


def _record(
    session: Session,
    snapshots: SnapshotStore,
    mode: RetentionMode,
    *,
    secrets: tuple[str, ...] = (),
) -> models.ModelInvocation:
    """One model call recorded under ``mode``."""
    recorder = make_recorder(
        session, snapshots, retention=RetentionPolicy(mode=mode, restricted=secrets)
    )
    run = recorder.start_run(project_id=seed_project(session))
    execution = recorder.start_stage(run, stage="extract_source_truth")
    return recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="ollama",
        model="llama3.1:70b-instruct",
        outcome=InvocationOutcome.ACCEPTED,
        raw_response=RAW,
        parsed_response=PARSED,
        validated_response=PARSED,
    )


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_the_six_modes_plan_13_names() -> None:
    """Exactly the six, so a deployment cannot invent a seventh silently."""
    assert {mode.value for mode in RetentionMode} == {
        "full",
        "redacted_full",
        "metadata_and_structured_only",
        "no_raw_provider_payloads",
        "temporary_raw_retention",
        "minimal_operational_logging",
    }


def test_the_default_is_detailed_and_said_out_loud() -> None:
    """plan/13: local-first may default to detailed retention — explicitly.

    A default is a choice somebody did not make, so it has to be the one that
    keeps the most: a trace can be thinned later and cannot be un-thinned.
    """
    assert RetentionPolicy().mode is RetentionMode.FULL


# ---------------------------------------------------------------------------
# What each mode keeps
# ---------------------------------------------------------------------------


def test_full_keeps_every_payload(db_session: Session, snapshot_store: SnapshotStore) -> None:
    invocation = _record(db_session, snapshot_store, RetentionMode.FULL)

    assert invocation.request_snapshot is not None
    assert invocation.raw_response_snapshot is not None
    assert invocation.parsed_response_snapshot is not None
    assert invocation.validated_response_snapshot is not None


def test_no_raw_provider_payloads_keeps_the_prompt_and_drops_the_response(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The raw provider payload is the one thing this mode exists to not keep.

    The prompt stays: it is what a replay needs, and it is material the project
    already owned before any provider saw it.
    """
    invocation = _record(db_session, snapshot_store, RetentionMode.NO_RAW_PROVIDER_PAYLOADS)

    assert invocation.request_snapshot is not None
    assert invocation.raw_response_snapshot is None
    assert invocation.parsed_response_snapshot is not None


def test_metadata_and_structured_only_drops_both_free_text_payloads(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Structured output survives; the prompt and the raw response do not."""
    invocation = _record(db_session, snapshot_store, RetentionMode.METADATA_AND_STRUCTURED_ONLY)

    assert invocation.request_snapshot is None
    assert invocation.raw_response_snapshot is None
    assert invocation.parsed_response_snapshot is not None
    assert invocation.validated_response_snapshot is not None


def test_minimal_operational_logging_keeps_no_payload_at_all(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    invocation = _record(db_session, snapshot_store, RetentionMode.MINIMAL_OPERATIONAL_LOGGING)

    assert invocation.request_snapshot is None
    assert invocation.raw_response_snapshot is None
    assert invocation.parsed_response_snapshot is None
    assert invocation.validated_response_snapshot is None


@pytest.mark.parametrize("mode", list(RetentionMode))
def test_the_call_itself_is_recorded_under_every_mode(
    db_session: Session, snapshot_store: SnapshotStore, mode: RetentionMode
) -> None:
    """The row is the record. No mode may make a call disappear.

    A trace that forgot a call happened would not be a smaller trace; it would
    be a wrong one, and every cost, latency and repair-rate number computed from
    it would be wrong too.
    """
    invocation = _record(db_session, snapshot_store, mode)

    assert invocation.provider == "ollama"
    assert invocation.model == "llama3.1:70b-instruct"
    assert invocation.outcome is InvocationOutcome.ACCEPTED
    assert invocation.started_at is not None


# ---------------------------------------------------------------------------
# redacted-full
# ---------------------------------------------------------------------------


def test_redacted_full_removes_the_projects_restricted_material(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The difference from ``full``, and the reason the mode exists.

    Everything is kept; the sensitive source material inside it is not. Secrets
    are already gone under every mode — that is plan/00's floor, not a setting.
    """
    invocation = _record(db_session, snapshot_store, RetentionMode.REDACTED_FULL, secrets=(PROMPT,))

    assert invocation.request_snapshot is not None
    stored = snapshot_store.read(invocation.request_snapshot).decode("utf-8")
    assert PROMPT not in stored
    assert "REDACTED" in stored


def test_full_keeps_the_material_redacted_full_would_remove(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Otherwise the two modes would be the same mode with two names."""
    invocation = _record(db_session, snapshot_store, RetentionMode.FULL, secrets=(PROMPT,))

    assert invocation.request_snapshot is not None
    stored = snapshot_store.read(invocation.request_snapshot).decode("utf-8")
    assert PROMPT in stored


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_temporary_raw_retention_keeps_the_raw_payload_at_first(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """It is *temporary*, not absent: a repair is diagnosed from the raw text."""
    invocation = _record(db_session, snapshot_store, RetentionMode.TEMPORARY_RAW_RETENTION)

    assert invocation.raw_response_snapshot is not None


def test_expiry_drops_raw_payloads_past_the_window(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/13 → expiration policies for raw provider payloads."""
    invocation = _record(db_session, snapshot_store, RetentionMode.TEMPORARY_RAW_RETENTION)
    later = invocation.started_at + RetentionPolicy().raw_payload_ttl + timedelta(minutes=1)

    expired = expire_raw_payloads(db_session, now=later)

    assert expired == 1
    assert invocation.raw_response_snapshot_id is None


def test_expiry_leaves_the_invocation_and_its_structured_output(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Expiring a payload is not deleting a record.

    What the model was asked, what it produced once parsed, what it cost and
    whether it was accepted all survive. Only the raw provider text goes.
    """
    invocation = _record(db_session, snapshot_store, RetentionMode.TEMPORARY_RAW_RETENTION)
    later = invocation.started_at + timedelta(days=365)

    expire_raw_payloads(db_session, now=later)

    assert invocation.request_snapshot is not None
    assert invocation.parsed_response_snapshot is not None
    assert invocation.outcome is InvocationOutcome.ACCEPTED


def test_expiry_leaves_a_payload_inside_the_window(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    invocation = _record(db_session, snapshot_store, RetentionMode.TEMPORARY_RAW_RETENTION)
    soon = invocation.started_at + timedelta(minutes=1)

    assert expire_raw_payloads(db_session, now=soon) == 0
    assert invocation.raw_response_snapshot_id is not None


def test_expiry_only_touches_runs_that_asked_for_it(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A ``full`` trace is not quietly thinned by a sweep meant for another run.

    The mode a call was recorded under travels with the call. Reading the
    project's *current* mode at sweep time would let a settings change rewrite
    history that was captured under a different promise.
    """
    invocation = _record(db_session, snapshot_store, RetentionMode.FULL)
    later = datetime.now(tz=UTC) + timedelta(days=365)

    assert expire_raw_payloads(db_session, now=later) == 0
    assert invocation.raw_response_snapshot_id is not None
