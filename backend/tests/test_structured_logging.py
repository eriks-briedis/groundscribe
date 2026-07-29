"""Logs that can be joined to the trace they describe (phase 14).

plan/14 → *structured logs correlate project / article / pipeline run / stage
execution / job / model request / tool invocation / trace event*.

The point of the requirement is a specific moment: something went wrong at
02:00, an operator has a log line, and they need the execution behind it. A line
that says ``job failed`` is a dead end. A line carrying the ids is a query.

Three properties, and they are what the tests are shaped around.

**Structured, not formatted.** One JSON object per line, so a field can be
searched on rather than parsed out of a sentence. A message that interpolated the
ids into prose would be greppable only by whoever wrote the sentence.

**An unknown id is absent, not null.** ``tool_invocation_id: null`` on every line
of a system that has never called a tool is noise that makes the field useless as
a filter. Present means known.

**Redaction happens before the line is handed to logging, not in the formatter.**
plan/00's rule is *removed before any trace, prompt, artefact or log is written —
never after*, and a formatter is the wrong place to keep it: a deployment that
attaches its own handler would then get the unredacted record. Redacting at the
call site means every handler, ours or not, receives material that is already
safe.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from sqlalchemy.orm import Session

from groundscribe.jobs.enums import JobStatus, JobType
from groundscribe.jobs.worker import JobOutcome, JobRequest, Worker
from groundscribe.observability.logging import (
    CORRELATION_FIELDS,
    Correlation,
    EventLogger,
    JSONFormatter,
    configure_logging,
)
from groundscribe.storage.snapshot_store import SnapshotStore
from job_helpers import make_queue, seed_run
from provenance_helpers import make_recorder


class Captured(logging.Handler):
    """A handler that keeps the formatted lines, as a stream would receive them."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JSONFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    @property
    def entries(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]


@pytest.fixture
def captured() -> Captured:
    return Captured()


@pytest.fixture
def log(captured: Captured) -> EventLogger:
    logger = logging.getLogger("groundscribe.test")
    logger.handlers = [captured]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return EventLogger(logger)


# ----------------------------------------------------------------------
# The vocabulary
# ----------------------------------------------------------------------


def test_the_correlation_vocabulary_is_the_eight_things_plan_14_names() -> None:
    """Named exhaustively, and in the order the pipeline produces them.

    A ninth id nobody agreed on would be a field half the system sets, which is
    worse than no field at all: a filter on it would silently exclude the
    components that did not know about it.
    """
    assert CORRELATION_FIELDS == (
        "project_id",
        "article_id",
        "pipeline_run_id",
        "stage_execution_id",
        "job_id",
        "model_invocation_id",
        "tool_invocation_id",
        "trace_event_id",
    )


# ----------------------------------------------------------------------
# What one line contains
# ----------------------------------------------------------------------


def test_a_line_carries_every_correlation_id_it_was_given(
    log: EventLogger, captured: Captured
) -> None:
    """plan/14 → *a sample log entry carries all correlation ids*."""
    correlation = Correlation(
        project_id="p1",
        article_id="a1",
        pipeline_run_id="r1",
        stage_execution_id="e1",
        job_id="j1",
        model_invocation_id="m1",
        tool_invocation_id="t1",
        trace_event_id="v1",
    )

    log.info("stage.completed", correlation, stage="generate_initial_draft")

    (entry,) = captured.entries
    assert entry["event"] == "stage.completed"
    assert entry["level"] == "INFO"
    assert entry["stage"] == "generate_initial_draft"
    assert {field: entry[field] for field in CORRELATION_FIELDS} == {
        "project_id": "p1",
        "article_id": "a1",
        "pipeline_run_id": "r1",
        "stage_execution_id": "e1",
        "job_id": "j1",
        "model_invocation_id": "m1",
        "tool_invocation_id": "t1",
        "trace_event_id": "v1",
    }
    # A timestamp a log aggregator can sort on rather than one it has to parse.
    assert entry["timestamp"].endswith("+00:00")


def test_an_id_nobody_knows_is_absent_rather_than_null(
    log: EventLogger, captured: Captured
) -> None:
    """Present means known. A field that is always there and usually null cannot
    be filtered on, which is the only thing it exists to support."""
    log.info("job.claimed", Correlation(job_id="j1", pipeline_run_id="r1"))

    (entry,) = captured.entries
    assert entry["job_id"] == "j1"
    assert "tool_invocation_id" not in entry
    assert "article_id" not in entry


def test_a_correlation_is_narrowed_rather_than_rebuilt(log: EventLogger) -> None:
    """A caller that knows one more id should be able to say so without restating
    the seven it was handed — the restating is where an id gets dropped."""
    run = Correlation(project_id="p1", pipeline_run_id="r1")
    stage = run.with_ids(stage_execution_id="e1")

    assert stage.as_dict() == {
        "project_id": "p1",
        "pipeline_run_id": "r1",
        "stage_execution_id": "e1",
    }
    # The original is untouched: a correlation is a value, and a caller holding
    # the run-level one must not find a stage id on it later.
    assert "stage_execution_id" not in run.as_dict()


# ----------------------------------------------------------------------
# Redaction
# ----------------------------------------------------------------------


def test_a_secret_in_a_log_field_never_reaches_a_handler(
    log: EventLogger, captured: Captured
) -> None:
    """plan/13 → secrets never appear in logs, and *before* the write, not after.

    Checked against the raw record rather than the formatted line: a redactor
    living in the formatter would pass this by inspection and still hand the
    secret to any other handler the deployment attached.
    """
    log.warning(
        "provider.rejected",
        Correlation(job_id="j1"),
        request="curl -H 'Authorization: Bearer sk-live-abcdefghijklmnopqrst'",
        api_key="sk-live-abcdefghijklmnopqrst",
    )

    (entry,) = captured.entries
    assert "sk-live-abcdefghijklmnopqrst" not in json.dumps(entry)
    assert "[REDACTED:" in entry["api_key"]
    assert "curl" in entry["request"], "redaction narrows the span, it does not delete the line"


def test_a_field_that_collides_with_a_correlation_id_cannot_overwrite_it(
    log: EventLogger, captured: Captured
) -> None:
    """The ids are the one part of the line an operator has to be able to trust.

    A caller passing ``job_id=`` as an ordinary field is almost certainly
    confused, and silently letting it win would make the correlation say
    something the system did not.
    """
    with pytest.raises(ValueError, match="job_id"):
        log.info("stage.started", Correlation(job_id="j1"), job_id="j2")


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


def test_configuring_logging_twice_does_not_double_every_line() -> None:
    """Both front doors configure logging on start-up, and a process that ran
    both — or a test that imported twice — would otherwise report everything
    twice, which reads as the system doing everything twice."""
    first = configure_logging()
    second = configure_logging()

    assert first is second
    assert len(first.handlers) == 1
    assert isinstance(first.handlers[0].formatter, JSONFormatter)


# ----------------------------------------------------------------------
# Wired to something that actually runs
# ----------------------------------------------------------------------


async def test_the_worker_logs_the_job_it_ran_with_the_ids_behind_it(
    db_session: Session, snapshot_store: SnapshotStore, captured: Captured
) -> None:
    """The requirement is only met if something real emits these lines.

    The worker is where it matters: it is the process that runs unattended, and
    the one whose failures are read from a log rather than watched on a screen.
    Every line it writes names the job, the run and — once the stage is open —
    the execution, which is exactly the path from "something failed overnight" to
    the records that say why.
    """
    recorder = make_recorder(db_session, snapshot_store)
    run = seed_run(db_session, snapshot_store, recorder)
    queue = make_queue(db_session)
    job = queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)

    async def handler(request: JobRequest) -> JobOutcome:
        request.opened(recorder.start_stage(run, stage="extract_source_truth", impl_version="1.1"))
        return JobOutcome(result={"snapshot_ids": ["s1"]})

    logger = logging.getLogger("groundscribe.jobs.worker")
    logger.handlers = [captured]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    worker = Worker(
        queue=queue,
        recorder=recorder,
        handlers={JobType.EXTRACT_SOURCE_MODEL: handler},
        worker_id="worker-1",
    )
    ran = await worker.run_once()

    assert ran is not None and ran.status is JobStatus.SUCCEEDED
    entries = captured.entries
    assert [entry["event"] for entry in entries] == ["job.claimed", "job.started", "job.completed"]
    assert {entry["job_id"] for entry in entries} == {job.id}
    assert all(entry["pipeline_run_id"] == run.id for entry in entries)
    assert all(entry["trace_event_id"] for entry in entries), (
        "a log line is a projection of the trace event beside it, and must name it"
    )
    # The execution appears from the moment it exists and not before: a line
    # claiming an execution the job had not opened yet would be an id that
    # resolves to nothing.
    assert "stage_execution_id" not in entries[0]
    assert entries[-1]["stage_execution_id"] == ran.stage_execution_id


async def test_a_failed_job_is_logged_at_a_level_that_wakes_somebody(
    db_session: Session, snapshot_store: SnapshotStore, captured: Captured
) -> None:
    """A job that failed is the line an operator is searching for at 02:00.

    Logged at ``ERROR`` with the error on the record, because the alternative —
    everything at ``INFO`` — means the only way to find a failure is to already
    know it happened.
    """
    recorder = make_recorder(db_session, snapshot_store)
    run = seed_run(db_session, snapshot_store, recorder)
    queue = make_queue(db_session)
    queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)

    async def handler(request: JobRequest) -> JobOutcome:
        raise RuntimeError("the provider refused")

    logger = logging.getLogger("groundscribe.jobs.worker")
    logger.handlers = [captured]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    worker = Worker(
        queue=queue,
        recorder=recorder,
        handlers={JobType.EXTRACT_SOURCE_MODEL: handler},
        worker_id="worker-1",
    )
    ran = await worker.run_once()

    assert ran is not None and ran.status is JobStatus.FAILED
    failure = captured.entries[-1]
    assert (failure["event"], failure["level"]) == ("job.failed", "ERROR")
    assert failure["error_type"] == "RuntimeError"
    assert failure["error_message"] == "the provider refused"
