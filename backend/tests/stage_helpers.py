"""Shared builders for the phase-06 editorial-stage tests.

Not a conftest, for the reason ``provenance_helpers`` is not one: an import error
while the subsystem is being built should fail the modules that use it, not
collection for the whole suite.

The context is assembled from the **shipped** prompt templates and the **shipped**
routing config, with only the transport faked. A stage test that rendered a
fixture prompt through a fixture route would prove the stage works against files
nobody ships; wiring the fake client under the provider name the real config
names keeps the whole chain — routing version, template version, recorded
provider/model — the one that runs in production.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArticleDepth
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.llm import FakeLLMClient, LLMClient
from groundscribe.llm.generation import StructuredGenerator
from groundscribe.llm.routing import default_routing_policy
from groundscribe.prompts import PromptStore, prompts_root
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.stages.base import PipelineContext
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.engine import WorkflowEngine
from groundscribe.workflow.states import WorkflowState
from provenance_helpers import make_recorder, seed_project

#: The provider the shipped routing config names for every local-first stage.
SHIPPED_PROVIDER = "ollama"


def fake_clients(model: str = "llama3.1:70b-instruct") -> dict[str, LLMClient]:
    """A client map keyed by the provider the shipped routing config asks for."""
    return {SHIPPED_PROVIDER: FakeLLMClient(provider=SHIPPED_PROVIDER, model=model)}


def build_generator(
    recorder: ProvenanceRecorder, clients: dict[str, LLMClient] | None = None
) -> StructuredGenerator:
    """A generator over the shipped prompts and the shipped routing policy."""
    return StructuredGenerator(
        clients=clients if clients is not None else fake_clients(),
        recorder=recorder,
        prompts=PromptStore(prompts_root()),
        routing=default_routing_policy(),
    )


#: The constraints a test project publishes under, permitting only the local
#: provider — which is what makes the provider-access check meaningful rather
#: than vacuous in every stage test that does not set out to exercise it.
DEFAULT_CONSTRAINTS = EditorialConstraints(
    audience="senior backend engineers",
    platform="personal blog",
    depth=ArticleDepth.PRACTITIONER,
    target_length_words=1800,
    allowed_providers=(SHIPPED_PROVIDER,),
    trace_retention_consent=True,
)


def build_context(
    session: Session,
    snapshots: SnapshotStore,
    *,
    clients: dict[str, LLMClient] | None = None,
    constraints: EditorialConstraints = DEFAULT_CONSTRAINTS,
    state: WorkflowState = WorkflowState.SOURCE_INGESTED,
) -> PipelineContext:
    """A pipeline context over a seeded project, a live run, and a fake transport."""
    project_id = seed_project(session)
    recorder = make_recorder(session, snapshots)
    run = recorder.start_run(project_id=project_id)
    engine = WorkflowEngine(recorder=recorder, snapshots=snapshots, run=run, state=state)
    return PipelineContext(
        engine=engine,
        recorder=recorder,
        snapshots=snapshots,
        generator=build_generator(recorder, clients),
        session=session,
        project_id=project_id,
        constraints=constraints,
    )
