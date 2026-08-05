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
from groundscribe.app.reads import ProjectionReader
from groundscribe.app.runtime import Runtime
from groundscribe.app.services import ApplicationService, CommandResult
from groundscribe.app.views import (
    ArchitectureBoard,
    ArticleWorkspace,
    LineageGraph,
    ProjectDashboard,
    ProjectIndex,
    QuestionQueue,
    ReviewHistory,
    SourceWorkspace,
    StageInspection,
    TraceFilter,
    TraceView,
)
from groundscribe.experiments.reproducibility import contract
from groundscribe.experiments.runs import ArmSpec
from groundscribe.experiments.variables import ForkRequest
from groundscribe.jobs.events import JobEventStream
from groundscribe.jobs.schemas import Job
from groundscribe.observability.metrics import RunMetrics
from groundscribe.privacy.export import ExportFormat
from groundscribe.provenance import models
from groundscribe.voice.schemas import VoiceProfileDocument

router = APIRouter()

#: Content type an ``EventSource`` requires; anything else and a browser will
#: not treat the response as a stream.
EVENT_STREAM = "text/event-stream"


def get_runtime(request: Request) -> Iterator[Runtime]:
    """The runtime for one request, closed when the request is over.

    Closing is not tidiness. A session holds its connection — and, on SQLite,
    the write lock — until the transaction ends, so a request that merely
    stopped referring to its session would keep the database against the worker
    until the garbage collector got round to it. The command path commits and so
    released it by accident; the read path, which commits nothing by design,
    never did.
    """
    runtime = request.app.state.runtime_factory()
    try:
        yield runtime
    finally:
        runtime.release()


def get_service(
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> Iterator[ApplicationService]:
    """One request, one transaction.

    The boundary belongs here rather than inside the service: a request is the
    unit a client can retry, so it is the unit that must either have happened or
    not. A command that raised leaves nothing half-written, and one that
    returned is durable by the time the response is sent.
    """
    service = ApplicationService(runtime)
    try:
        yield service
    except Exception:
        service.rollback()
        raise
    service.commit()


#: The two dependencies every route needs, named once. ``Annotated`` rather than
#: a default argument: a ``Depends(...)`` in a default is evaluated at import and
#: reads as a shared mutable default to everything that is not FastAPI.
Service = Annotated[ApplicationService, Depends(get_service)]
RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def get_reader_runtime(request: Request) -> Iterator[Runtime]:
    """A runtime for a request that will not write, closed when the request ends.

    Its own factory, and therefore on SQLite its own kind of transaction: a
    deferred one, which proceeds against the last committed snapshot instead of
    queueing behind whatever the worker is in the middle of. A screen that took
    the command path's transaction would go dark for the length of a model call
    (KNOWN-ISSUES §1) while asking for nothing it could not already have.
    """
    runtime = request.app.state.reader_factory()
    try:
        yield runtime
    finally:
        runtime.release()


def get_reader(
    runtime: Annotated[Runtime, Depends(get_reader_runtime)],
) -> ProjectionReader:
    """The read side, on a read-only runtime, outside the command's transaction.

    Deliberately not built from :func:`get_service`: a read commits nothing
    because it writes nothing, and taking the commit-on-return dependency would
    imply otherwise (phase 11 → *a read changes nothing*). That contract is what
    makes the deferred transaction above safe to give it.
    """
    return ProjectionReader(runtime)


Reader = Annotated[ProjectionReader, Depends(get_reader)]

#: A runtime for a route that reads without going through the projections — the
#: event stream, which needs the session itself. On the read side because it is a
#: read, and because a stream that took the write lock between polls would be
#: unable to follow the very job that was holding it.
ReadingRuntimeDep = Annotated[Runtime, Depends(get_reader_runtime)]

#: The trace filters, as repeated ``?filter=`` values. Typed as the enum so a
#: name the system does not know is refused by the schema rather than dropped —
#: a person shown everything after asking for failures only would draw
#: conclusions from a list they did not request.
TraceFilters = Annotated[list[TraceFilter] | None, Query(alias="filter")]


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


@router.get("/projects", response_model=ProjectIndex)
def read_projects(reader: Reader) -> ProjectIndex:
    """Every project this installation holds — the way into everything else."""
    return reader.projects()


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
)
def answer_gap(
    project_id: str,
    gap_id: str,
    body: schemas.AnswerGap,
    service: Service,
) -> schemas.CommandResponse:
    """Record one answer. The run stays in the queue until the round is submitted."""
    return render(
        service.answer_gap(
            project_id,
            gap_id=gap_id,
            text=body.text,
            answered_by=body.answered_by,
            response=body.response,
        )
    )


@router.post(
    "/projects/{project_id}/retry", response_model=schemas.CommandResponse, status_code=202
)
def retry_failed_job(
    project_id: str, body: schemas.ActorAction, service: Service
) -> schemas.CommandResponse:
    """Queue the work that failed under this run, again. Moves the run nowhere."""
    return render(service.retry_failed_job(project_id, requested_by=body.actor_id))


@router.post(
    "/projects/{project_id}/source-questions/submit",
    response_model=schemas.CommandResponse,
    status_code=202,
)
def submit_answers(
    project_id: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.CommandResponse:
    """Hand the answered round back, rebuilding the source model from all of it."""
    return render(service.submit_answers(project_id, submitted_by=body.actor_id))


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
    "/projects/{project_id}/architecture/proposal/abandon",
    response_model=schemas.CommandResponse,
)
def abandon_proposal(
    project_id: str, body: schemas.ActorAction, service: Service
) -> schemas.CommandResponse:
    """Give up on the proposal in flight and keep the approved architecture.

    Declared above the ``{version}`` routes so "proposal" is read as itself
    rather than as an architecture version — FastAPI matches in declaration
    order, and the literal has to win.
    """
    return render(service.abandon_proposal(project_id, requested_by=body.actor_id))


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


@router.post(
    "/articles/{article_id}/approve-and-continue",
    response_model=schemas.CommandResponse,
    status_code=202,
)
def approve_and_continue(
    article_id: str,
    body: schemas.ContinueToArticle,
    service: Service,
) -> schemas.CommandResponse:
    """Publish this article, then brief another the architecture approved.

    ``next_article_id`` is in the body rather than inferred, because the run
    cannot know it: auto-advance follows the article the *architecture* selected,
    which is the one being finished here, and which of the remaining concepts is
    worth writing is a judgement only the author holds.
    """
    return render(
        service.approve_and_continue(
            article_id,
            approved_by=body.actor_id,
            next_article_id=body.next_article_id,
        )
    )


@router.post(
    "/articles/{article_id}/revise", response_model=schemas.CommandResponse, status_code=202
)
def revise(
    article_id: str,
    body: schemas.ReviseArticle,
    service: Service,
) -> schemas.CommandResponse:
    """Send a failed score to the stage that can correct it.

    The pause at ``revision_required`` is deliberate — it is where a person may
    accept the article anyway. This is the other way out of it, and until now
    there was none: the routing policy and its seven destinations were reachable
    from nothing but a test.
    """
    return render(service.revise(article_id, requested_by=body.actor_id, prefer=body.prefer))


@router.post("/articles/{article_id}/override-approve", response_model=schemas.CommandResponse)
def override_and_approve(
    article_id: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.CommandResponse:
    """Accept an article the score refused, on a person's explicit say-so."""
    return render(service.override_and_approve(article_id, approved_by=body.actor_id))


# ----------------------------------------------------------------------
# Reads: one per screen (phase 11)
#
# Every one of these is a ``GET`` that touches nothing. They exist because the
# interface is artefact-first: a command says where the run is, and a screen has
# to show what the run has made. The assembly lives in the app layer, so the CLI
# can ask the same questions; the routes below only choose the URL.
# ----------------------------------------------------------------------


@router.get("/projects/{project_id}/dashboard", response_model=ProjectDashboard)
def read_dashboard(project_id: str, reader: Reader) -> ProjectDashboard:
    """Where the project stands: state, source, articles, jobs, failures, cost."""
    return reader.dashboard(project_id)


@router.get("/projects/{project_id}/source-workspace", response_model=SourceWorkspace)
def read_source_workspace(project_id: str, reader: Reader) -> SourceWorkspace:
    """The source, what was extracted from it, and who may see it."""
    return reader.source_workspace(project_id)


@router.get("/projects/{project_id}/questions", response_model=QuestionQueue)
def read_questions(project_id: str, reader: Reader) -> QuestionQueue:
    """Every question the source raised, answered ones included."""
    return reader.questions(project_id)


@router.get("/projects/{project_id}/architecture", response_model=ArchitectureBoard)
def read_architecture(project_id: str, reader: Reader) -> ArchitectureBoard:
    """The proposed shape of the work, every version of it."""
    return reader.architecture(project_id)


@router.get("/projects/{project_id}/trace", response_model=TraceView)
def read_trace(project_id: str, reader: Reader, filter: TraceFilters = None) -> TraceView:
    """The run's executions, narrowed to what a filter names.

    ``filter`` shadows the builtin, and keeps the name because it is the query
    string a person types; the alias is the API's vocabulary, not Python's.
    """
    return reader.trace(project_id, filters=filter or ())


@router.get("/articles/{article_id}/workspace", response_model=ArticleWorkspace)
def read_article_workspace(article_id: str, reader: Reader) -> ArticleWorkspace:
    """Everything needed to judge the current version, approval included."""
    return reader.article_workspace(article_id)


@router.get("/articles/{article_id}/reviews", response_model=ReviewHistory)
def read_review_history(article_id: str, reader: Reader) -> ReviewHistory:
    """The rounds, the scores they earned, and what each finding did."""
    return reader.review_history(article_id)


@router.get("/articles/{article_id}/lineage", response_model=LineageGraph)
def read_lineage(article_id: str, reader: Reader) -> LineageGraph:
    """How each version came from the one before it."""
    return reader.lineage(article_id)


@router.get("/executions/{execution_id}/inspect", response_model=StageInspection)
def inspect_execution(execution_id: str, reader: Reader) -> StageInspection:
    """One execution, with every layer phase 03 recorded for it."""
    return reader.inspect(execution_id)


# ----------------------------------------------------------------------
# Voice (phase 10)
# ----------------------------------------------------------------------


@router.post("/voice/profiles", response_model=schemas.VoiceProfileSummary, status_code=201)
def save_voice_profile(
    body: VoiceProfileDocument,
    service: Service,
    user_id: Annotated[str, Query(description="Whose voice this is.")],
    project_id: Annotated[str | None, Query()] = None,
    article_id: Annotated[str | None, Query()] = None,
) -> schemas.VoiceProfileSummary:
    """Put a profile version in force at its scope.

    The body is the profile *document* itself rather than an API-shaped wrapper.
    It is what the author edits and what the voice pass consumes, and a second
    shape in between would be a place for the two to disagree — including about
    whether a hard rule names anything checkable, which the document validates
    and a wrapper would have to remember to.
    """
    return schemas.VoiceProfileSummary.model_validate(
        service.save_voice_profile(
            body, user_id=user_id, project_id=project_id, article_id=article_id
        )
    )


@router.get("/voice/profiles", response_model=list[schemas.VoiceProfileSummary])
def list_voice_profiles(
    service: Service, user_id: Annotated[str, Query()]
) -> list[schemas.VoiceProfileSummary]:
    """Every version this author has saved, in force or superseded."""
    return [
        schemas.VoiceProfileSummary.model_validate(version)
        for version in service.voice_profiles(user_id=user_id)
    ]


@router.get("/projects/{project_id}/voice", response_model=schemas.EffectiveVoice)
def read_effective_voice(
    project_id: str,
    service: Service,
    article_id: Annotated[str | None, Query()] = None,
) -> schemas.EffectiveVoice:
    """The voice in force here, and where each instruction came from."""
    user_id = service.author_of(project_id)
    resolved = service.effective_voice(
        user_id=user_id, project_id=project_id, article_id=article_id
    )
    return schemas.EffectiveVoice(
        sources=resolved.sources,
        active=tuple(
            schemas.ActiveInstructionOut.model_validate(entry) for entry in resolved.record()
        ),
        suppressed=tuple(item.instruction.id for item in resolved.suppressed),
    )


@router.get("/voice/suggestions", response_model=list[schemas.VoiceSuggestionOut])
def list_voice_suggestions(
    service: Service, user_id: Annotated[str, Query()]
) -> list[schemas.VoiceSuggestionOut]:
    """Inferred rules still waiting for an answer. Listing applies nothing."""
    return [
        schemas.VoiceSuggestionOut.model_validate(suggestion)
        for suggestion in service.voice_suggestions(user_id=user_id)
    ]


@router.post(
    "/voice/suggestions/{suggestion_id}/approve", response_model=schemas.VoiceProfileSummary
)
def approve_voice_suggestion(
    suggestion_id: str, body: schemas.ApproveSuggestion, service: Service
) -> schemas.VoiceProfileSummary:
    """Make an inferred rule permanent. The only endpoint that changes a voice."""
    return schemas.VoiceProfileSummary.model_validate(
        service.approve_voice_suggestion(
            suggestion_id, approved_by=body.actor_id, version=body.version
        )
    )


@router.post("/voice/suggestions/{suggestion_id}/reject", response_model=schemas.VoiceSuggestionOut)
def reject_voice_suggestion(
    suggestion_id: str, body: schemas.RejectSuggestion, service: Service
) -> schemas.VoiceSuggestionOut:
    """Record that the author said no, and why."""
    return schemas.VoiceSuggestionOut.model_validate(
        service.reject_voice_suggestion(
            suggestion_id, rejected_by=body.actor_id, reason=body.reason
        )
    )


# ----------------------------------------------------------------------
# Executions
# ----------------------------------------------------------------------


@router.get("/executions/compare", response_model=schemas.ExecutionComparison)
def compare_executions(
    service: Service,
    reader: Reader,
    left: Annotated[str, Query()],
    right: Annotated[str, Query()],
) -> schemas.ExecutionComparison:
    """Two executions side by side. Declared before ``/executions/{id}`` so the
    literal path is matched first rather than read as an execution called
    "compare"."""
    first, second = service.compare_executions(left, right)
    differences, distance = reader.comparison(first, second)
    return schemas.ExecutionComparison(
        left=schemas.ExecutionSummary.model_validate(first),
        right=schemas.ExecutionSummary.model_validate(second),
        differences=differences,
        output_edit_distance=distance,
        # Carried with the comparison rather than looked up beside it: plan/12's
        # risk is a misleading reproducibility claim, and this is the screen
        # where one gets made.
        reproducibility=[schemas.GuaranteeOut.model_validate(item) for item in contract()],
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
    "/executions/{execution_id}/replay", response_model=schemas.RerunResponse, status_code=202
)
def replay_execution(
    execution_id: str,
    body: schemas.ActorAction,
    service: Service,
) -> schemas.RerunResponse:
    """Queue the stage again, exactly as it ran. The original is never touched."""
    rerun = service.replay_execution(execution_id, requested_by=body.actor_id)
    return schemas.RerunResponse(
        source_execution_id=rerun.source_execution_id, job=Job.model_validate(rerun.job)
    )


@router.post(
    "/executions/{execution_id}/fork", response_model=schemas.RerunResponse, status_code=202
)
def fork_execution(
    execution_id: str,
    body: ForkRequest,
    service: Service,
) -> schemas.RerunResponse:
    """Run the stage again with something changed (phase 12).

    The body's ``variables`` are a closed vocabulary, so a name the system
    cannot act on is refused here — by the schema, with a 422 — rather than
    producing an experiment whose candidate configuration was never applied.
    """
    rerun = service.fork_execution(
        execution_id,
        requested_by=body.actor_id,
        variables=body.variables,
        reason=body.reason,
    )
    return schemas.RerunResponse(
        source_execution_id=rerun.source_execution_id, job=Job.model_validate(rerun.job)
    )


# ----------------------------------------------------------------------
# Experiments and jobs
# ----------------------------------------------------------------------


@router.get("/reproducibility", response_model=list[schemas.GuaranteeOut])
def read_reproducibility() -> list[schemas.GuaranteeOut]:
    """What repeating work here does and does not guarantee (plan/12).

    An endpoint rather than documentation because the question is asked while
    looking at two executions, not while reading a README.
    """
    return [schemas.GuaranteeOut.model_validate(item) for item in contract()]


@router.post("/evaluation-datasets", response_model=schemas.DatasetOut, status_code=201)
def build_dataset(body: schemas.BuildDataset, service: Service) -> schemas.DatasetOut:
    """Build an evaluation corpus out of the runs a person approved."""
    return schemas.DatasetOut.model_validate(
        service.build_dataset(
            name=body.name,
            created_by=body.created_by,
            description=body.description,
            include_sensitive=body.include_sensitive,
        )
    )


@router.get("/evaluation-datasets", response_model=list[schemas.DatasetOut])
def list_datasets(service: Service) -> list[schemas.DatasetOut]:
    """Every corpus built so far."""
    return [schemas.DatasetOut.model_validate(dataset) for dataset in service.datasets()]


@router.get("/evaluation-datasets/{dataset_id}", response_model=schemas.DatasetOut)
def read_dataset(dataset_id: str, service: Service) -> schemas.DatasetOut:
    """One corpus, with the examples it holds."""
    return schemas.DatasetOut.model_validate(service.dataset(dataset_id))


@router.post("/experiments", response_model=schemas.ExperimentOut, status_code=201)
def create_experiment(body: schemas.CreateExperiment, service: Service) -> schemas.ExperimentOut:
    """Open an experiment over one corpus, with the configurations to compare."""
    experiment = service.create_experiment(
        name=body.name,
        dataset_id=body.dataset_id,
        created_by=body.created_by,
        description=body.description,
        arms=[
            ArmSpec(label=arm.label, baseline=arm.baseline, variables=arm.variables)
            for arm in body.arms
        ],
    )
    return _experiment_out(service, experiment)


@router.post(
    "/experiments/{experiment_id}/start",
    response_model=list[schemas.ExperimentResultOut],
    status_code=202,
)
def start_experiment(experiment_id: str, service: Service) -> list[schemas.ExperimentResultOut]:
    """Queue every arm against every example.

    Answers with the pending results rather than the comparison: an experiment
    over a corpus is the longest-running thing this system does, and a request
    that waited for it would be a request that times out.
    """
    return [
        schemas.ExperimentResultOut.model_validate(result)
        for result in service.start_experiment(experiment_id)
    ]


@router.get("/experiments/{experiment_id}", response_model=schemas.ExperimentReportOut)
def read_experiment(experiment_id: str, service: Service) -> schemas.ExperimentReportOut:
    """One experiment: its arms, every per-example result, and the table."""
    report = service.experiment_report(experiment_id)
    return schemas.ExperimentReportOut(
        experiment=schemas.ExperimentOut(
            **schemas.ExperimentOut.model_validate(report.experiment).model_dump(exclude={"arms"}),
            arms=[schemas.ArmOut.model_validate(arm) for arm in report.arms],
        ),
        results=[schemas.ExperimentResultOut.model_validate(item) for item in report.results],
        comparison=list(report.comparison),
    )


@router.post(
    "/experiments/{experiment_id}/preferences",
    response_model=schemas.PreferenceOut,
    status_code=201,
)
def record_preference(
    experiment_id: str, body: schemas.RecordPreference, service: Service
) -> schemas.PreferenceOut:
    """Record which arm a person judged better on one example."""
    return schemas.PreferenceOut.model_validate(
        service.prefer_arm(
            experiment_id,
            entry_id=body.entry_id,
            arm_id=body.arm_id,
            decided_by=body.decided_by,
            reason=body.reason,
        )
    )


def _experiment_out(service: Service, experiment: models.ExperimentRun) -> schemas.ExperimentOut:
    """One experiment with its arms attached, which is how a client reads it."""
    report = service.experiment_report(experiment.id)
    return schemas.ExperimentOut(
        **schemas.ExperimentOut.model_validate(experiment).model_dump(exclude={"arms"}),
        arms=[schemas.ArmOut.model_validate(arm) for arm in report.arms],
    )


@router.get("/jobs/{job_id}", response_model=Job)
def read_job(job_id: str, service: Service) -> Job:
    """One job, for a client that would rather poll than stream."""
    return Job.model_validate(service.job(job_id))


@router.get("/jobs/{job_id}/events")
def stream_job_events(
    job_id: str,
    runtime: ReadingRuntimeDep,
    after: Annotated[int, Query(description="Last event sequence already seen.")] = -1,
) -> StreamingResponse:
    """Stream a job's progress until it finishes.

    ``after`` is a sequence number rather than a count, so a client that
    reconnects resumes from what it saw instead of replaying the run — the same
    contract as SSE's ``Last-Event-ID``, which phase 03's stored event ordering
    already supports.
    """
    # The session belongs to this request and only this stream uses it, so the
    # stream may end its transaction between polls — which it must, or watching
    # a job would hold the database against the worker running it.
    stream = JobEventStream(runtime.session, runtime.queue, release=runtime.session.rollback)

    async def frames() -> AsyncIterator[str]:
        async for event in stream.stream(job_id, after=after):
            yield event.encode()

    return StreamingResponse(frames(), media_type=EVENT_STREAM)


__all__ = [
    "EVENT_STREAM",
    "get_reader",
    "get_runtime",
    "get_service",
    "render",
    "router",
]


# ----------------------------------------------------------------------
# Privacy and export (phase 13)
# ----------------------------------------------------------------------


@router.get("/versions/{version_id}/export", response_model=schemas.ExportedArticleOut)
def export_version(
    version_id: str, service: Service, format: ExportFormat = ExportFormat.MARKDOWN
) -> schemas.ExportedArticleOut:
    """One article version, rendered in a named format (plan/13).

    Addressed by version rather than by article: what a person exports is the
    version that passed validation, and naming it is what makes exporting the
    wrong one impossible rather than merely unlikely. The bytes are read back
    from the store and hash-checked before anything is rendered.
    """
    return schemas.ExportedArticleOut.model_validate(service.render_version(version_id, format))


@router.get(
    "/projects/{project_id}/provider-visibility",
    response_model=schemas.ProviderVisibilityOut,
)
def read_provider_visibility(project_id: str, service: Service) -> schemas.ProviderVisibilityOut:
    """Where this project's material goes, and what is kept of it (plan/13).

    Counts and routes only. A screen that displayed the confidential passages in
    order to warn about them would be the leak it was drawn to prevent.
    """
    return schemas.ProviderVisibilityOut.model_validate(service.provider_visibility(project_id))


@router.put(
    "/projects/{project_id}/routing-profile",
    response_model=schemas.CommandResponse,
)
def set_routing_profile(
    project_id: str,
    body: schemas.SetRoutingProfile,
    service: Service,
) -> schemas.CommandResponse:
    """Point this project's stages at a routing profile, or back at the default.

    ``PUT``, not ``POST``: the body states what the profile *is*, and sending it
    twice leaves the project where sending it once did. Not 202 either — nothing
    is queued, and the change applies to the next stage that runs.
    """
    return render(
        service.set_routing_profile(project_id, profile=body.profile, chosen_by=body.actor_id)
    )


@router.get("/projects/{project_id}/traces", response_model=schemas.TraceExportOut)
def export_project_traces(
    project_id: str,
    service: Service,
    sanitise: bool = False,
    confidential_material_acknowledged: bool = False,
) -> schemas.TraceExportOut:
    """This project's execution records (plan/13).

    A full export of a project holding confidential material is refused — 409,
    via the status map — unless the caller acknowledges it in the request. The
    guard has to live here rather than in a warning field on a 200 response: by
    the time anyone could read such a field, the bytes have already been sent.
    """
    return schemas.TraceExportOut.model_validate(
        service.export_traces(
            project_id,
            sanitise=sanitise,
            confidential_material_acknowledged=confidential_material_acknowledged,
        )
    )


@router.delete("/projects/{project_id}/traces", response_model=schemas.TraceDeletionOut)
def delete_project_traces(project_id: str, service: Service) -> schemas.TraceDeletionOut:
    """Drop this project's stored payloads, keeping the record of what ran."""
    return schemas.TraceDeletionOut.model_validate(service.delete_traces(project_id))


# ----------------------------------------------------------------------
# Observability (phase 14)
# ----------------------------------------------------------------------


@router.get("/metrics", response_model=RunMetrics)
def read_installation_metrics(service: Service) -> RunMetrics:
    """What the whole installation has done and spent (plan/14).

    Its own route rather than the per-project one with the id left off: this is
    the question a monitoring check asks on a schedule, and expressing it as a
    project id would mean inventing a sentinel value that every future filter
    then has to remember to exclude.
    """
    return service.metrics()


@router.get("/projects/{project_id}/metrics", response_model=RunMetrics)
def read_project_metrics(project_id: str, service: Service) -> RunMetrics:
    """The same seventeen numbers, for one project (plan/14).

    The collector's own model is the response model. A second schema here could
    drift from it, and the two would then disagree about the same installation —
    which is the failure that makes an operator stop believing any of it.
    """
    return service.metrics(project_id)


@router.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Whether this process is up (plan/14).

    Liveness and nothing else — no counts, no ids, no configuration. It is the
    one route reachable without a session, because a container orchestrator has
    no credential and never will, and it must therefore be worth nothing to an
    unauthenticated caller beyond what the open socket already told them.

    Deliberately not a readiness check either. Touching the database here would
    make a busy SQLite file (KNOWN-ISSUES §1) read as a dead container and
    restart the process that was doing the work.
    """
    return {"status": "ok", "service": "groundscribe"}
