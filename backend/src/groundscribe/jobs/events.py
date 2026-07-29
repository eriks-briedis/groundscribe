"""Per-job progress, as a server-sent event stream (phase 09).

plan/09 → *SSE progress stream per job/execution*.

The stream is assembled from the trace, not published alongside it. A job's
progress *is* the trace of the stage execution it opened — the model attempts,
the repairs, the decisions — so what a client watches live and what an auditor
reads a month later are the same rows. Two channels would eventually disagree,
and the one the user saw would be the one nobody could reconstruct.

Around those stored events sit two synthesised frames the trace cannot supply:

- an opening ``job.status``, so a client connecting late knows where the job is
  rather than inferring it from what arrives next;
- a closing ``job.status`` when the job reaches a terminal state, because a
  stream that merely stopped is indistinguishable from a network that did.

Resumption falls out of phase 03: every trace event carries a stored position in
its run's timeline, which is exactly what SSE's ``Last-Event-ID`` needs. A
reconnecting client resumes from what it saw instead of replaying the run.

Sleep is injected. A poll loop written against the real clock is slow when the
tests pass and flaky when they fail.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.jobs.models import Job
from groundscribe.jobs.queue import JobQueue
from groundscribe.provenance import models

#: How long to wait between reads of the trace. Short enough to feel live,
#: long enough that a browser left open on a finished project is not a load
#: generator.
DEFAULT_POLL_SECONDS = 0.5

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class ProgressEvent:
    """One frame of the stream, ready to be encoded for ``text/event-stream``.

    ``sequence`` is the event's position in the run's timeline, and becomes the
    SSE ``id``. Synthesised status frames have no position — they are not part
    of the run's history — so they carry no id and a reconnecting client
    resumes from the last *real* event instead.
    """

    event: str
    data: dict[str, Any] = field(default_factory=dict)
    sequence: int | None = None

    def encode(self) -> str:
        """The wire format an ``EventSource`` reads."""
        lines = []
        if self.sequence is not None:
            lines.append(f"id: {self.sequence}")
        lines.append(f"event: {self.event}")
        lines.append(f"data: {json.dumps(self.data)}")
        return "\n".join(lines) + "\n\n"


class JobEventStream:
    """Reads a job's progress out of the trace, and keeps reading until it ends."""

    def __init__(
        self,
        session: Session,
        queue: JobQueue,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sleep: Sleeper | None = None,
        release: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._queue = queue
        self._poll = poll_seconds
        self._sleep = sleep or asyncio.sleep
        self._release = release

    async def stream(self, job_id: str, *, after: int = -1) -> AsyncGenerator[ProgressEvent, None]:
        """Yield the job's events from ``after`` onwards, ending when it does.

        ``after`` is a sequence number, not a count: the client tells the server
        the last position it saw, and everything strictly newer follows. That is
        the only resumption rule that survives a reconnection landing on a
        different process.
        """
        job = self._queue.get(job_id)
        if job is None:
            # Not an error. A client may reasonably ask about a job that has
            # been pruned, or mistype an id; an empty stream says "nothing to
            # watch" without pretending something went wrong.
            return

        yield _status_of(job)
        cursor = after
        while True:
            for event in self._events_since(job, cursor):
                cursor = event.sequence
                yield _from_trace(event)

            self._session.refresh(job)
            if job.status.is_terminal:
                break
            # Let go of the database before waiting. What this stream waits *for*
            # is the worker committing to it, so a loop that held its transaction
            # would be waiting on something it had itself made impossible.
            #
            # Only when the caller said it may: the session is borrowed, and a
            # caller that has uncommitted work in it — a test setting up
            # fixtures, an in-process caller mid-transaction — would lose it. The
            # request that serves this stream owns its session and is read-only,
            # which is why the route passes a release and nobody else does.
            if self._release is not None:
                self._release()
            await self._sleep(self._poll)

        for event in self._events_since(job, cursor):
            cursor = event.sequence
            yield _from_trace(event)
        yield _status_of(job)

    def _events_since(self, job: Job, cursor: int) -> list[models.TraceEvent]:
        """The stage's trace events after ``cursor``, oldest first.

        Scoped to the job's own stage execution rather than to the run: a run has
        many jobs, and a stream that showed all of them would report another
        command's progress as this one's.
        """
        if job.stage_execution_id is None:
            return []
        return list(
            self._session.scalars(
                select(models.TraceEvent)
                .where(
                    models.TraceEvent.stage_execution_id == job.stage_execution_id,
                    models.TraceEvent.sequence > cursor,
                )
                .order_by(models.TraceEvent.sequence)
            )
        )


def _status_of(job: Job) -> ProgressEvent:
    """The job's own state, as the frame that opens and closes the stream."""
    data: dict[str, Any] = {
        "job_id": job.id,
        "job_type": job.job_type,
        "status": job.status.value,
        "stage_execution_id": job.stage_execution_id,
    }
    if job.error_type is not None:
        data["error_type"] = job.error_type
        data["error_message"] = job.error_message
    if job.status.is_terminal:
        data["result"] = job.result
    return ProgressEvent(event="job.status", data=data)


def _from_trace(event: models.TraceEvent) -> ProgressEvent:
    """One stored trace event, as a frame.

    The payload is nested under its own key rather than merged into the frame:
    a stage is free to record whatever it needs, and a merge would let a payload
    field quietly shadow ``event_type`` or ``timestamp``.
    """
    return ProgressEvent(
        event=event.event_type,
        sequence=event.sequence,
        data={
            "event_id": event.id,
            "event_type": event.event_type,
            "timestamp": event.timestamp.isoformat(),
            "actor_type": event.actor_type.value,
            "actor_id": event.actor_id,
            "payload": event.payload,
        },
    )


__all__ = ["DEFAULT_POLL_SECONDS", "JobEventStream", "ProgressEvent"]
