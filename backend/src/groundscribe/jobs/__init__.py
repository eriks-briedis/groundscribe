"""The database-backed job queue and the worker that drains it (phase 09).

plan/09 → *Background execution*: model work leaves the HTTP request lifecycle
and runs in a separate process. The queue is a table, not a framework — the
plan's non-goals rule out Dramatiq, Celery and Temporal, and a system that
already owns a transactional database owns everything a reliable queue needs.
"""

from __future__ import annotations

from groundscribe.jobs.enums import JobStatus, JobType
from groundscribe.jobs.models import Job
from groundscribe.jobs.queue import JobQueue

__all__ = ["Job", "JobQueue", "JobStatus", "JobType"]
