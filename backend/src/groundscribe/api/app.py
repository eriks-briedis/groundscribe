"""The FastAPI application (phase 09).

plan/09 → *command endpoints, `available_actions`, OpenAPI as the contract
source of truth*.

Two things are decided here and nowhere else.

**Every route is a translation.** It reads a request body, calls one service
method, and renders the result. No route decides whether a transition is legal,
which stage runs, or what happens next — those questions have one answer each,
in the workflow engine, and a route that formed a second opinion would be the
"API embedding workflow rules" the plan explicitly forbids.

**Failures keep the distinction the domain already draws.** An illegal
transition is 409: the request is fine and the *run* is in the wrong state, so a
client that fixes its JSON gets the same answer while one that waits may not. An
unattributed human action is 422 — the payload is what is wrong. A missing
project, article or execution is 404 wherever it was noticed. A database held by
a running job is 503, because waiting is the whole remedy. Letting any of these
reach the client as a 500 would turn a precise refusal into "something broke".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from groundscribe.api import auth
from groundscribe.api.routes import router
from groundscribe.app.reads import UnknownArtefact
from groundscribe.app.rehydrate import MissingInput
from groundscribe.app.runtime import Runtime
from groundscribe.app.services import (
    NothingToAbandon,
    NothingToRetry,
    NothingToRevise,
    UndecidableFinding,
    UnknownFinding,
    UnknownProject,
)
from groundscribe.experiments.datasets import SensitiveProject
from groundscribe.experiments.replay import NotRerunnable
from groundscribe.experiments.runs import IncomparableExperiment, UnknownArm
from groundscribe.llm.routing import RoutingConfigError
from groundscribe.privacy.export import ExportIntegrityError
from groundscribe.privacy.traces import ConfidentialExportRefused
from groundscribe.workflow.errors import (
    ArtifactProvenanceError,
    AttributionRequired,
    ConfidentialMaterialError,
    ExportMismatchError,
    HumanActionRequired,
    IllegalTransition,
    LineageError,
    SilentMutationError,
)
from groundscribe.workflow.policy import WorkflowPolicyError

#: How the app gets its collaborators. A callable rather than a fixed instance
#: because a real deployment builds one per request around a fresh session,
#: while a test hands back the one bound to its rolled-back transaction.
RuntimeFactory = Callable[[], Runtime]

TITLE = "groundscribe"
VERSION = "0.1.0"
DESCRIPTION = (
    "A local-first, inspectable editorial workflow. Every command returns where "
    "the run now is and what may be done to it next; work that calls a model is "
    "queued for a worker rather than done in the request."
)

#: Domain failures and the status each deserves. Mapped as data so the reason for
#: a code is stated once, next to the exception that earns it.
_STATUS_FOR: tuple[tuple[type[Exception], int], ...] = (
    # The run is in the wrong state. Retrying later may work; retrying now will not.
    (IllegalTransition, 409),
    (HumanActionRequired, 409),
    # An approved artefact would be silently replaced, or the wrong version
    # exported. Both are conflicts with what the run has already committed to.
    (SilentMutationError, 409),
    (ExportMismatchError, 409),
    (LineageError, 409),
    (ArtifactProvenanceError, 409),
    # The request itself is unusable: nobody is accountable for the action.
    (AttributionRequired, 422),
    # Or it named a destination the routing policy does not permit for that
    # failure. 422 rather than 409: the run's state is fine and the *request* is
    # what cannot be honoured — a factual failure is corrected against source
    # truth, and asking to rewrite it instead is asking for the wrong thing.
    (WorkflowPolicyError, 422),
    # Asked to run failed work again where there is none, or where it is already
    # coming. 409 rather than 404: the run exists and the request is well formed,
    # and what makes it unanswerable is the run's state — which changes.
    (NothingToRetry, 409),
    # Asked to route a score that passed, or an article never scored. 409 for the
    # same reason: the run exists, the request is fine, and the run's state is
    # what makes it unanswerable.
    (NothingToRevise, 409),
    # Asked to give up on a proposal by a run with no approved architecture to
    # fall back to. 409 for the same reason again, and the message names the
    # command that does work there.
    (NothingToAbandon, 409),
    # Asked to decide a finding this review does not hold, or to set one to a
    # status a person does not choose.
    (UnknownFinding, 404),
    (UndecidableFinding, 422),
    # Something named does not exist.
    (UnknownProject, 404),
    (MissingInput, 404),
    (UnknownArtefact, 404),
    # Asked to repeat something that was never queued, or whose inputs are gone.
    # 409: the request is well formed and the *execution* cannot support it.
    (NotRerunnable, 409),
    # Phase 12. An experiment described in a way that cannot produce a comparison
    # is a bad payload (422); a judgement filed against an arm that does not
    # exist names something missing (404).
    (IncomparableExperiment, 422),
    # Phase 13. A full trace export of a project holding confidential material,
    # without the caller saying it means to. 409 rather than 403: the request is
    # permitted, it conflicts with what the project has declared about itself,
    # and re-issuing it with the acknowledgement succeeds.
    (ConfidentialExportRefused, 409),
    # Publishing an article that reprints flagged material, or a version whose
    # stored bytes no longer match their recorded hash.
    (ConfidentialMaterialError, 409),
    (ExportIntegrityError, 409),
    (SensitiveProject, 422),
    (UnknownArm, 404),
    # Phase 15. A routing profile that is not a name, or names no file on this
    # installation. 422 rather than 404: what is missing is not the thing the URL
    # addressed — the project exists — but the value in the body, and the fix is
    # to send a different one or to add the file.
    (RoutingConfigError, 422),
)

#: What a database says when it is *held*, rather than broken.
#:
#: Matched on the driver's own words because both supported databases report
#: contention through the same exception class they use for everything else:
#: SQLite as a locked database, PostgreSQL as a lock timeout or a deadlock. The
#: alternative — treating every ``OperationalError`` as contention — would tell
#: someone whose schema is missing to try again, forever.
CONTENTION_MARKERS: tuple[str, ...] = (
    "database is locked",
    "database table is locked",
    "lock timeout",
    "deadlock detected",
)

#: How long a client is asked to wait. Shorter than the busy timeout it just sat
#: through, because the holder is a job that may take minutes and a person who
#: retries early learns that sooner than one who is told to wait for a number
#: nobody can predict.
RETRY_AFTER_SECONDS = 5

#: Written here rather than taken from the exception. SQLAlchemy's message is a
#: SQL statement and a link to its own documentation; the person reading this is
#: looking at a dashboard that just refused them.
BUSY_DETAIL = (
    "the database is busy — a pipeline job is holding it while it runs. Nothing "
    "was changed; issue the command again once the job finishes."
)


def create_app(
    *,
    runtime_factory: RuntimeFactory,
    reader_factory: RuntimeFactory | None = None,
    password: str | None = None,
) -> FastAPI:
    """Build the API around a way of getting a runtime.

    ``reader_factory`` is how a deployment hands the read side a runtime that
    will not take a write lock (see :func:`~groundscribe.app.bootstrap.build_runtime`).
    It defaults to ``runtime_factory``, so a caller with one session — every test
    in the suite — keeps the behaviour it has: the distinction is a property of
    the *database*, and one shared in-memory connection has no contention to
    protect anyone from.

    ``password`` locks it. Given one, every request outside ``/auth`` must carry
    a session cookie issued by :mod:`groundscribe.api.auth`; given ``None``, the
    application is open.

    Open is the default because the *library* has no business demanding a
    credential — the suite builds hundreds of these to test something else
    entirely, and a mandatory password would mean every one of them carrying a
    secret to say nothing about security. The danger of the default is handled
    where the application is actually served: :mod:`groundscribe.api.asgi`
    refuses to start without one.
    """
    app = FastAPI(title=TITLE, version=VERSION, description=DESCRIPTION)
    app.state.runtime_factory = runtime_factory
    app.state.reader_factory = reader_factory or runtime_factory
    app.state.password = password
    app.include_router(auth.router)
    app.include_router(router)

    if password is not None:
        app.middleware("http")(_require_session)

    for exception_type, status in _STATUS_FOR:
        app.add_exception_handler(exception_type, _handler(status))
    app.add_exception_handler(OperationalError, _contention_handler)
    return app


async def _require_session(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Refuse anything outside ``/auth`` that arrives without a valid session.

    Middleware rather than a dependency, deliberately: a dependency protects the
    routes that remember to ask for it, and the failure mode of that is an
    endpoint added next month that quietly is not protected. This protects
    everything, including paths that do not exist — a 401 for an unknown URL
    also tells an unauthenticated caller nothing about what is served here.
    """
    password: str | None = request.app.state.password
    if request.url.path.startswith(auth.PUBLIC_PREFIXES):
        return await call_next(request)
    if password is not None and not auth.session_is_valid(
        request.cookies.get(auth.SESSION_COOKIE), password
    ):
        return JSONResponse(status_code=401, content={"detail": "sign in first"})
    return await call_next(request)


def _handler(status: int) -> Callable[[Request, Exception], JSONResponse]:
    """Render one domain failure as the status it earns, with its own message.

    The message is the exception's own. Every one of these is written for a
    person — "``approve_architecture`` is not available in ``source_ingested``
    (offered: …)" — and replacing it with a generic string would throw away the
    most useful thing the domain produced.
    """

    def handle(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    return handle


def _contention_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Report a database somebody else is holding as busy, and nothing else.

    A worker holds SQLite's write lock for the length of the job it is running
    (KNOWN-ISSUES §1), which with a real provider is the length of a model call.
    Every command issued during that waits out the busy timeout and then fails —
    a failure of *timing*, where the request was fine, nothing was written, and
    the remedy is the one thing a 500 does not suggest.

    Anything else wearing the same exception class is re-raised unchanged. A
    missing table is not a condition that clears while a person waits, and this
    handler saying so would hide the only useful thing about it.
    """
    origin = getattr(exc, "orig", exc)
    if not any(marker in str(origin).lower() for marker in CONTENTION_MARKERS):
        raise exc
    return JSONResponse(
        status_code=503,
        content={"detail": BUSY_DETAIL},
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


__all__ = [
    "BUSY_DETAIL",
    "CONTENTION_MARKERS",
    "DESCRIPTION",
    "RETRY_AFTER_SECONDS",
    "TITLE",
    "VERSION",
    "RuntimeFactory",
    "create_app",
]
