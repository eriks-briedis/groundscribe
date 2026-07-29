"""Structured logs that can be joined back to the trace (phase 14).

plan/14 → *structured logs correlate project / article / pipeline run / stage
execution / job / model request / tool invocation / trace event*.

The requirement exists for one moment. Something failed overnight, an operator
has a line of log, and they need the execution behind it. A line reading ``job
failed`` is a dead end; the same line carrying the eight ids is a query against
the provenance tables, which is where the answer already is.

**A log line is a projection of the trace, never a second record of it.** The
same position :mod:`groundscribe.observability.metrics` takes, for the same
reason: two independent records of one event can disagree, and this system's
proposition is that the trace is what happened. So the ids are what a line is
*for*, and the line itself carries no history the trace does not already hold. A
deployment that ships its logs off the machine loses nothing but convenience.

**An unknown id is absent, not null.** A ``tool_invocation_id: null`` on every
line of an installation that has never called a tool is noise that makes the
field useless as a filter, which is the only thing it exists to support.

**Redaction happens here, before ``logging`` sees the record.** plan/00 requires
secrets removed *before* anything is written, and a formatter is the wrong place
to hold that line: a deployment attaching its own handler — a syslog shipper, a
JSON aggregator — would then receive the unredacted record and our formatter
would never run. Redacting at the call site means every handler, ours or not,
gets material that is already safe.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import IO, Any

from groundscribe.provenance.redaction import Redactor

#: The correlation vocabulary, in the order the pipeline produces the ids.
#:
#: Exhaustive and named: a component that invented a ninth would produce a field
#: only it sets, and a filter on that field would silently exclude everything
#: else — worse than not having it.
CORRELATION_FIELDS: tuple[str, ...] = (
    "project_id",
    "article_id",
    "pipeline_run_id",
    "stage_execution_id",
    "job_id",
    "model_invocation_id",
    "tool_invocation_id",
    "trace_event_id",
)

#: The root every groundscribe logger hangs from, so one handler configures all
#: of them and a deployment can raise or silence the whole application by name.
LOGGER_ROOT = "groundscribe"

#: Keys the formatter writes itself. A caller's field may not take one of these
#: either, for the reason a correlation id may not be taken: a line whose
#: ``level`` was supplied by the code being logged says nothing reliable.
RESERVED_FIELDS = frozenset({"timestamp", "level", "logger", "event", *CORRELATION_FIELDS})


@dataclass(frozen=True)
class Correlation:
    """The ids that say which run, stage, job and call a line is about.

    A value, not a context: it is passed explicitly and narrowed by copying, so
    two concurrent jobs in one process cannot end up sharing one. An ambient
    context variable would be tidier at the call sites and would attribute a
    line to the wrong run exactly when the system is busiest.
    """

    project_id: str | None = None
    article_id: str | None = None
    pipeline_run_id: str | None = None
    stage_execution_id: str | None = None
    job_id: str | None = None
    model_invocation_id: str | None = None
    tool_invocation_id: str | None = None
    trace_event_id: str | None = None

    def with_ids(self, **ids: str | None) -> Correlation:
        """A copy that also knows ``ids``.

        Narrowing rather than rebuilding: a caller that has learnt one more id
        should not have to restate the seven it was handed, because restating is
        where one of them gets dropped.
        """
        unknown = set(ids) - set(CORRELATION_FIELDS)
        if unknown:
            raise ValueError(f"not correlation ids: {', '.join(sorted(unknown))}")
        return replace(self, **ids)

    def as_dict(self) -> dict[str, str]:
        """Only the ids that are known. Absent means unknown, never null."""
        return {
            field: value
            for field in CORRELATION_FIELDS
            if isinstance(value := getattr(self, field), str) and value
        }


class JSONFormatter(logging.Formatter):
    """One JSON object per line: searchable by field rather than by sentence."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            # An aware ISO-8601 instant, so a log aggregator sorts rather than
            # parses — and so two machines in different zones interleave
            # correctly, which is the whole reason phase 03 refused naive
            # timestamps in the trace.
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        entry.update(getattr(record, "groundscribe_fields", {}))
        if record.exc_info:
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, sort_keys=False)


class EventLogger:
    """Emits correlated, redacted events onto an ordinary :mod:`logging` logger.

    A wrapper rather than a :class:`logging.LoggerAdapter` subclass: the adapter
    contract is "rewrite the message and the kwargs", and what is wanted here is
    a different call signature — an event name, a correlation, and fields — which
    reads as itself rather than as an adapter bent into shape.
    """

    def __init__(self, logger: logging.Logger, *, redactor: Redactor | None = None) -> None:
        self._logger = logger
        self._redactor = redactor or Redactor()

    def info(self, event: str, correlation: Correlation | None = None, **fields: Any) -> None:
        self.log(logging.INFO, event, correlation, **fields)

    def warning(self, event: str, correlation: Correlation | None = None, **fields: Any) -> None:
        self.log(logging.WARNING, event, correlation, **fields)

    def error(self, event: str, correlation: Correlation | None = None, **fields: Any) -> None:
        self.log(logging.ERROR, event, correlation, **fields)

    def log(
        self,
        level: int,
        event: str,
        correlation: Correlation | None = None,
        **fields: Any,
    ) -> None:
        """Write one event, with its ids, having removed anything secret first."""
        collisions = RESERVED_FIELDS & set(fields)
        if collisions:
            raise ValueError(
                f"{', '.join(sorted(collisions))} may not be passed as a field: "
                "the correlation ids are the part of a line that has to be trusted"
            )
        ids = correlation.as_dict() if correlation is not None else {}
        self._logger.log(
            level,
            event,
            extra={"groundscribe_fields": {**ids, **self._redactor.redact_payload(fields)}},
        )


def event_logger(name: str, *, redactor: Redactor | None = None) -> EventLogger:
    """The correlated logger for one module, under the application's root."""
    return EventLogger(logging.getLogger(name), redactor=redactor)


def configure_logging(
    *, level: int = logging.INFO, stream: IO[str] | None = None
) -> logging.Logger:
    """Install the JSON handler on the application's root logger, once.

    Idempotent because both front doors configure logging on start-up and a
    process that ran both — an API importing the CLI, a test importing either —
    would otherwise report every line twice, which reads as the system doing
    everything twice.

    Only groundscribe's own tree is touched, and only by *adding*. Reconfiguring
    the root logger would reformat every library in the process, and replacing
    this logger's handlers would silently detach whatever else attached one — a
    deployment's shipper, a test's capture — which is a surprising thing for a
    function called "configure" to do.
    """
    logger = logging.getLogger(LOGGER_ROOT)
    logger.setLevel(level)
    if not any(isinstance(handler.formatter, JSONFormatter) for handler in logger.handlers):
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    # Ours is the only handler that should format these lines; propagating would
    # hand the record to the root logger's default handler as well and print
    # every event a second time, unstructured.
    logger.propagate = False
    return logger


__all__ = [
    "CORRELATION_FIELDS",
    "LOGGER_ROOT",
    "RESERVED_FIELDS",
    "Correlation",
    "EventLogger",
    "JSONFormatter",
    "configure_logging",
    "event_logger",
]
