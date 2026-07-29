"""The command endpoints (phase 09).

plan/09 lists them; this is that list, one route per command, each of them a
translation and nothing more. The pattern is uniform on purpose: read the body,
call one service method, render the envelope. A reader should be able to see at a
glance that no route decides anything.

Status codes carry meaning:

- **202** for a command that queued work. It has accepted the request, not
  completed it, and the job in the body is what the client watches.
- **200** for one that finished in the request — every deterministic command, and
  every read.
- **201** for the two endpoints that create something addressable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from groundscribe.api import schemas
from groundscribe.app.runtime import Runtime
from groundscribe.app.services import ApplicationService, CommandResult
from groundscribe.jobs.events import JobEventStream
from groundscribe.jobs.schemas import Job

router = APIRouter()

#: Content type an ``EventSource`` requires; anything else and a browser will
#: not treat the response as a stream.
EVENT_STREAM = "text/event-stream"


def get_runtime(request: Request) -> Iterator[Runtime]:
    """The runtime for one request, from whatever the app was built with."""
    yield request.app.state.runtime_factory()


def get_service(runtime: Annotated[Runtime, Depends(get_runtime)]) -> ApplicationService:
    return ApplicationService(runtime)


#: The two dependencies every route needs, named once. ``Annotated`` rather than
#: a default argument: a ``Depends(...)`` in a default is evaluated at import and
#: reads as a shared mutable default to everything that is not FastAPI.
Service = Annotated[ApplicationService, Depends(get_service)]
RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def render(result: CommandResult) -> schemas.CommandResponse:
    """One envelope for every command, whatever it did."""
    return schemas.CommandResponse(
        project_id=result.project_id,
        run_id=result.run_id,
        state=result.state,
        available_actions=result.available_actions,
        job=Job.model_validate(result.job) if result.job is not None else None,
        detail=result.detail,
    )


# ----------------------------------------------------------------------
# Projects and sources
# ----------------------------------------------------------------------


@router.post("/projects", response_model=schemas.CommandResponse, status_code=201)
def create_project(body: schemas.CreateProject, service: Service) -> schemas.CommandResponse:
    """Open a project, its bounds, its run and its position."""
    return render(
        service.create_project(
            title=body.title,
            author_id=body.author_id,
            constraints=body.constraints,
            description=body.description,
        )
    )


@router.get("/projects/{project_id}", response_model=schemas.CommandResponse)
def read_project(project_id: str, service: Service) -> schemas.CommandResponse:
    """Where the run is and what may be done to it, changing nothing."""
    return render(service.project_state(project_id))


@router.post("/projects/{project_id}/sources", response_model=schemas.CommandResponse)
async def import_source(
    project_id: str,
    body: schemas.ImportSource,
    service: Service,
) -> schemas.CommandResponse:
    """Store one piece of source material. No model is involved, so no job."""
    return render(
        await service.import_source(
            project_id,
            title=body.title,
            text=body.text,
            source_format=body.source_format,
            confidential=body.confidential,
            uri=body.uri,
        )
    )


@router.post("/projects/{project_id}/cancel", response_model=schemas.CommandResponse)
def cancel(
    project_id: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.CommandResponse:
    """Stop the run. Available in every non-terminal state, so none is a trap."""
    return render(service.cancel(project_id, cancelled_by=body.actor_id))


# ----------------------------------------------------------------------
# Source model
# ----------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/source-model/extract",
    response_model=schemas.CommandResponse,
    status_code=202,
)
async def extract_source_model(
    project_id: str,
    body: schemas.ExtractSourceModel,
    service: Service,
) -> schemas.CommandResponse:
    """Queue the source-model build, and the gap analysis that follows it."""
    return render(await service.extract_source_model(project_id, token_budget=body.token_budget))


@router.post(
    "/projects/{project_id}/source-gaps/{gap_id}/answer",
    response_model=schemas.CommandResponse,
    status_code=202,
)
def answer_gap(
    project_id: str,
    gap_id: str,
    body: schemas.AnswerGap,
    service: Service,
) -> schemas.CommandResponse:
    """Record an answer and rebuild the source model from it."""
    return render(
        service.answer_gap(
            project_id,
            gap_id=gap_id,
            text=body.text,
            answered_by=body.answered_by,
            response=body.response,
        )
    )


# ----------------------------------------------------------------------
# Architecture
# ----------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/architecture/propose",
    response_model=schemas.CommandResponse,
    status_code=202,
)
async def propose_architecture(project_id: str, service: Service) -> schemas.CommandResponse:
    """Queue a proposal of the article or series the source supports."""
    return render(await service.propose_architecture(project_id))


@router.put("/projects/{project_id}/architecture/{version}", response_model=schemas.CommandResponse)
def update_architecture(
    project_id: str,
    version: str,
    body: schemas.UpdateArchitecture,
    service: Service,
) -> schemas.CommandResponse:
    """Commit an author's edits as a new version.

    ``version`` addresses the architecture the author was looking at. The service
    edits the version currently in force; a mismatch is a conflict the engine's
    lineage guard raises, which is the check that actually matters.
    """
    return render(
        service.update_architecture(
            project_id,
            commands=body.commands,
            requested_by=body.requested_by,
            reason=body.reason,
            accepted_warnings=body.accepted_warnings,
        )
    )


@router.post(
    "/projects/{project_id}/architecture/{version}/approve",
    response_model=schemas.CommandResponse,
)
def approve_architecture(
    project_id: str,
    version: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.CommandResponse:
    """Lock the architecture, and open an article for each approved concept."""
    return render(service.approve_architecture(project_id, approved_by=body.actor_id))


# ----------------------------------------------------------------------
# Articles
# ----------------------------------------------------------------------


@router.post(
    "/articles/{article_id}/brief/generate",
    response_model=schemas.CommandResponse,
    status_code=202,
)
async def generate_brief(article_id: str, service: Service) -> schemas.CommandResponse:
    """Queue the brief this article will be drafted against."""
    return render(await service.generate_brief(article_id))


@router.post("/articles/{article_id}/brief/approve", response_model=schemas.CommandResponse)
def approve_brief(
    article_id: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.CommandResponse:
    """Accept the brief, which is what opens drafting."""
    return render(service.approve_brief(article_id, approved_by=body.actor_id))


@router.post(
    "/articles/{article_id}/draft", response_model=schemas.CommandResponse, status_code=202
)
async def draft(article_id: str, service: Service) -> schemas.CommandResponse:
    """Queue the first draft."""
    return render(await service.draft(article_id))


@router.post(
    "/articles/{article_id}/review", response_model=schemas.CommandResponse, status_code=202
)
async def review(article_id: str, service: Service) -> schemas.CommandResponse:
    """Queue a substantive review of the current version."""
    return render(await service.review(article_id))


@router.post(
    "/articles/{article_id}/revision-plan",
    response_model=schemas.CommandResponse,
    status_code=202,
)
async def plan_revision(article_id: str, service: Service) -> schemas.CommandResponse:
    """Queue the plan a rewrite will be bound by."""
    return render(await service.plan_revision(article_id))


@router.post("/articles/{article_id}/revision-plan/approve", response_model=schemas.CommandResponse)
def approve_revision_plan(
    article_id: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.CommandResponse:
    """Authorise the rewrite the plan describes."""
    return render(service.approve_revision_plan(article_id, approved_by=body.actor_id))


@router.post(
    "/articles/{article_id}/rewrite", response_model=schemas.CommandResponse, status_code=202
)
async def rewrite(article_id: str, service: Service) -> schemas.CommandResponse:
    """Queue the rewrite."""
    return render(await service.rewrite(article_id))


@router.post(
    "/articles/{article_id}/voice-align", response_model=schemas.CommandResponse, status_code=202
)
async def voice_align(article_id: str, service: Service) -> schemas.CommandResponse:
    """Queue the voice pass."""
    return render(await service.voice_align(article_id))


@router.post(
    "/articles/{article_id}/score", response_model=schemas.CommandResponse, status_code=202
)
async def score(article_id: str, service: Service) -> schemas.CommandResponse:
    """Queue the scoring pass, which is also what routes a failure."""
    return render(await service.score(article_id))


@router.post("/articles/{article_id}/validate", response_model=schemas.CommandResponse)
async def validate(article_id: str, service: Service) -> schemas.CommandResponse:
    """Run final validation here and now: it calls no model (plan/08)."""
    return render(await service.validate(article_id))


@router.post("/articles/{article_id}/approve", response_model=schemas.CommandResponse)
def approve(
    article_id: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.CommandResponse:
    """The author publishes the validated article."""
    return render(service.approve(article_id, approved_by=body.actor_id))


@router.post("/articles/{article_id}/override-approve", response_model=schemas.CommandResponse)
def override_and_approve(
    article_id: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.CommandResponse:
    """Accept an article the score refused, on a person's explicit say-so."""
    return render(service.override_and_approve(article_id, approved_by=body.actor_id))


# ----------------------------------------------------------------------
# Executions
# ----------------------------------------------------------------------


@router.get("/executions/compare", response_model=schemas.ExecutionComparison)
def compare_executions(
    service: Service,
    left: Annotated[str, Query()],
    right: Annotated[str, Query()],
) -> schemas.ExecutionComparison:
    """Two executions side by side. Declared before ``/executions/{id}`` so the
    literal path is matched first rather than read as an execution called
    "compare"."""
    first, second = service.compare_executions(left, right)
    return schemas.ExecutionComparison(
        left=schemas.ExecutionSummary.model_validate(first),
        right=schemas.ExecutionSummary.model_validate(second),
    )


@router.get("/executions/{execution_id}", response_model=schemas.ExecutionSummary)
def read_execution(execution_id: str, service: Service) -> schemas.ExecutionSummary:
    """One stage execution, with how it ended."""
    return schemas.ExecutionSummary.model_validate(service.get_execution(execution_id))


@router.get("/executions/{execution_id}/events", response_model=list[schemas.TraceEventOut])
def read_execution_events(execution_id: str, service: Service) -> list[schemas.TraceEventOut]:
    """The execution's timeline, in the order it was recorded."""
    return [
        schemas.TraceEventOut.model_validate(event)
        for event in service.execution_events(execution_id)
    ]


@router.get(
    "/executions/{execution_id}/invocations", response_model=list[schemas.ModelInvocationOut]
)
def read_execution_invocations(
    execution_id: str, service: Service
) -> list[schemas.ModelInvocationOut]:
    """Every model call the execution made, failed attempts included."""
    return [
        schemas.ModelInvocationOut.model_validate(invocation)
        for invocation in service.execution_invocations(execution_id)
    ]


@router.post(
    "/executions/{execution_id}/replay", response_model=schemas.ExecutionSummary, status_code=201
)
def replay_execution(
    execution_id: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.ExecutionSummary:
    """Re-run a stage as a new execution. The original is never touched."""
    return schemas.ExecutionSummary.model_validate(
        service.replay_execution(execution_id, requested_by=body.actor_id)
    )


@router.post(
    "/executions/{execution_id}/fork", response_model=schemas.ExecutionSummary, status_code=201
)
def fork_execution(
    execution_id: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.ExecutionSummary:
    """Branch a new execution from an existing one."""
    return schemas.ExecutionSummary.model_validate(
        service.fork_execution(execution_id, requested_by=body.actor_id)
    )


# ----------------------------------------------------------------------
# Experiments and jobs
# ----------------------------------------------------------------------


@router.post("/experiments", response_model=schemas.ExperimentOut, status_code=201)
def create_experiment(body: schemas.CreateExperiment, service: Service) -> schemas.ExperimentOut:
    """Open an experiment record; phase 12 fills in what it means."""
    return schemas.ExperimentOut.model_validate(service.create_experiment(name=body.name))


@router.get("/jobs/{job_id}", response_model=Job)
def read_job(job_id: str, service: Service) -> Job:
    """One job, for a client that would rather poll than stream."""
    return Job.model_validate(service.job(job_id))


@router.get("/jobs/{job_id}/events")
def stream_job_events(
    job_id: str,
    runtime: RuntimeDep,
    after: Annotated[int, Query(description="Last event sequence already seen.")] = -1,
) -> StreamingResponse:
    """Stream a job's progress until it finishes.

    ``after`` is a sequence number rather than a count, so a client that
    reconnects resumes from what it saw instead of replaying the run — the same
    contract as SSE's ``Last-Event-ID``, which phase 03's stored event ordering
    already supports.
    """
    stream = JobEventStream(runtime.session, runtime.queue)

    async def frames() -> AsyncIterator[str]:
        async for event in stream.stream(job_id, after=after):
            yield event.encode()

    return StreamingResponse(frames(), media_type=EVENT_STREAM)


__all__ = ["EVENT_STREAM", "get_runtime", "get_service", "render", "router"]
