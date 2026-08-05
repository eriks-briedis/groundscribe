"""What the trace costs, and the report a person can show someone (phase 13).

Spec (plan/13 → *Trace-storage controls: compression, dedup (content addressing
from phase 02), retention policies, storage-use reporting*; and *sanitised
execution report for debugging/portfolio*).

Two small things that finish the phase's deliverables, and one decision recorded
rather than built.

**Storage-use reporting** is what makes the other three controls actionable.
Retention modes and expiry both trade evidence for space, and nobody can make
that trade without knowing what the space is. The report says how much, in what,
and how much deduplication has already saved — that last number being the one
that decides whether compression is worth having at all.

**Compression is deliberately not implemented.** The store already deduplicates
by content address, the payloads are small JSON documents, and adding a
compression layer would change the on-disk format for a saving nobody has
measured. The report is what would produce that measurement; when it says
something, the decision can be revisited with evidence. This is recorded in
KNOWN-ISSUES rather than quietly skipped.

**The sanitised execution report** is the trace export rendered for a human
reader. Same content, different document: JSON is what a tool consumes, and
plan/13's use for this is a debugging thread or a portfolio, where the reader is
a person.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from groundscribe.cli import main as cli
from groundscribe.privacy.storage import storage_report
from groundscribe.privacy.traces import export_traces
from groundscribe.provenance.enums import InvocationOutcome
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import EffectiveRequest
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import make_recorder, seed_project

PROMPT = "Summarise the March cache postmortem for a senior audience."


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _record(recorder: ProvenanceRecorder, project_id: str, *, prompt: str = PROMPT) -> None:
    run = recorder.start_run(project_id=project_id)
    execution = recorder.start_stage(run, stage="extract_source_truth")
    recorder.record_model_invocation(
        execution,
        request=EffectiveRequest(
            template_id="extract_source_truth", template_version="1", rendered_prompt=prompt
        ),
        provider="ollama",
        model="llama3.1:70b-instruct",
        outcome=InvocationOutcome.ACCEPTED,
        raw_response='{"summary":"a cache cut latency"}',
    )
    recorder.complete_stage(execution)


# ---------------------------------------------------------------------------
# Storage use
# ---------------------------------------------------------------------------


def test_the_report_says_how_much_and_in_what(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A number with no breakdown is a number nobody can act on.

    "Your traces are 4GB" prompts no decision; "3.9GB of it is raw provider
    payloads" prompts exactly one, and it is a retention mode away.
    """
    recorder = make_recorder(db_session, snapshot_store)
    _record(recorder, seed_project(db_session))

    report = storage_report(db_session)

    assert report.total_bytes > 0
    assert report.snapshots > 0
    assert report.by_type["effective_request"].bytes > 0
    assert report.by_type["raw_response"].count == 1


def test_the_report_says_what_deduplication_already_saved(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The number that decides whether compression is worth having.

    Two identical requests are two snapshot rows and one blob. Reporting only
    the sum of snapshot sizes would overstate the cost of the store by exactly
    the amount content addressing is already saving.
    """
    recorder = make_recorder(db_session, snapshot_store)
    project_id = seed_project(db_session)
    _record(recorder, project_id)
    _record(recorder, project_id)

    report = storage_report(db_session)

    assert report.snapshots == 4
    assert report.distinct_blobs == 2
    assert report.deduplicated_bytes > 0
    assert report.stored_bytes < report.total_bytes


def test_a_report_over_one_project_counts_only_that_project(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Storage is charged to whoever is deciding what to do about it."""
    recorder = make_recorder(db_session, snapshot_store)
    mine = seed_project(db_session, user_id="u-mine", project_id="p-mine")
    theirs = seed_project(db_session, user_id="u-theirs", project_id="p-theirs")
    _record(recorder, mine)
    _record(recorder, theirs, prompt="An entirely different and much longer prompt " * 20)

    whole = storage_report(db_session)
    just_mine = storage_report(db_session, project_id=mine)

    assert just_mine.total_bytes < whole.total_bytes
    assert just_mine.snapshots < whole.snapshots


def test_an_empty_installation_reports_zero_rather_than_failing(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The first thing a new user runs should not be the first thing that breaks."""
    report = storage_report(db_session)

    assert report.total_bytes == 0
    assert report.snapshots == 0
    assert report.deduplicated_bytes == 0


# ---------------------------------------------------------------------------
# The sanitised execution report
# ---------------------------------------------------------------------------


def test_the_report_reads_as_a_document(db_session: Session, snapshot_store: SnapshotStore) -> None:
    """plan/13's use for this is a debugging thread or a portfolio.

    Both have a person at the other end, and a person is not the reader JSON was
    designed for.
    """
    recorder = make_recorder(db_session, snapshot_store)
    project_id = seed_project(db_session)
    _record(recorder, project_id)

    report = export_traces(db_session, snapshot_store, project_id, sanitise=True).to_report()

    assert report.startswith("# Execution report")
    assert "extract_source_truth" in report
    assert "llama3.1:70b-instruct" in report
    assert "sanitised" in report


def test_the_report_withholds_what_the_export_withheld(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A different rendering is not a second policy.

    Rendering from the export rather than from the database is what makes that
    structural: the report cannot show what the export did not carry.
    """
    recorder = make_recorder(db_session, snapshot_store)
    project_id = seed_project(db_session)
    _record(recorder, project_id)

    report = export_traces(db_session, snapshot_store, project_id, sanitise=True).to_report()

    assert PROMPT not in report


def test_a_full_report_says_that_it_is_not_sanitised(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A reader deciding whether to paste this somewhere needs to be told which.

    The two documents look alike; only one of them is safe to share, and the
    difference must not be something the reader has to infer.
    """
    recorder = make_recorder(db_session, snapshot_store)
    project_id = seed_project(db_session)
    _record(recorder, project_id)

    report = export_traces(db_session, snapshot_store, project_id).to_report()

    assert "not sanitised" in report


def test_the_report_and_the_cost_are_both_reachable(cli_runner: CliRunner) -> None:
    """Built and unreachable is the defect phase 12 had to record (KNOWN-ISSUES §4).

    A storage figure nobody can ask for cannot inform a retention decision, and
    a report nobody can render is a rendering nobody uses.
    """
    # Width pinned, because Rich wraps help text to the terminal it thinks it has
    # and an option name is the first thing a narrow one breaks across lines.
    # Unpinned, this passed on a developer's terminal and failed in CI, which
    # makes it a test of the window rather than of the command.
    wide = {"COLUMNS": "200"}

    privacy = cli_runner.invoke(cli.app, ["privacy", "--help"], env=wide).output
    assert "report" in privacy

    traces = cli_runner.invoke(cli.app, ["privacy", "traces", "--help"], env=wide).output
    assert "--report" in traces
