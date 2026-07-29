"""Building the application layer for tests (phase 09).

Not a conftest, for the reason ``provenance_helpers`` is not one.

The runtime is assembled from the **shipped** prompts and the **shipped** routing
config with only the transport faked, exactly as ``stage_helpers`` does for the
editorial stages. A service test that ran through fixture prompts would prove the
service works against files nobody ships.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from groundscribe.app.handlers import stage_handlers
from groundscribe.app.runtime import Runtime
from groundscribe.app.services import ApplicationService
from groundscribe.jobs.models import Job
from groundscribe.jobs.queue import JobQueue
from groundscribe.jobs.worker import Worker
from groundscribe.llm import FakeLLMClient
from groundscribe.llm.generation import StructuredGenerator
from groundscribe.llm.routing import default_routing_policy
from groundscribe.prompts import PromptStore, prompts_root
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.position import PositionStore
from provenance_helpers import make_recorder, sequential_ids
from stage_helpers import SHIPPED_PROVIDER

#: The author every test attributes its human actions to.
AUTHOR = "ada"


@dataclass(frozen=True)
class Harness:
    """A service, a worker over the same rows, and the fake behind both."""

    service: ApplicationService
    worker: Worker
    client: FakeLLMClient
    runtime: Runtime

    async def drain(self) -> tuple[Job, ...]:
        """Run everything currently queued, as a worker process would."""
        return await self.worker.run_until_idle()


def build_harness(session: Session, snapshots: SnapshotStore) -> Harness:
    """An application layer over a rolled-back session and a scripted model."""
    client = FakeLLMClient(provider=SHIPPED_PROVIDER, model="gpt-5")
    recorder = make_recorder(session, snapshots)
    runtime = Runtime(
        session=session,
        snapshots=snapshots,
        recorder=recorder,
        generator=StructuredGenerator(
            clients={SHIPPED_PROVIDER: client},
            recorder=recorder,
            prompts=PromptStore(prompts_root()),
            routing=default_routing_policy(),
        ),
        queue=JobQueue(session, id_factory=sequential_ids("job")),
        positions=PositionStore(session, id_factory=sequential_ids("pos")),
        # Lent, not given: the test keeps reading through this session after the
        # request that used it has returned.
        owns_session=False,
    )
    service = ApplicationService(runtime)
    return Harness(
        service=service,
        worker=Worker(
            queue=runtime.queue,
            recorder=recorder,
            handlers=stage_handlers(runtime),
            worker_id="worker-1",
        ),
        client=client,
        runtime=runtime,
    )


__all__ = ["AUTHOR", "Harness", "build_harness"]
