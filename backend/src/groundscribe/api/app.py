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
project, article or execution is 404 wherever it was noticed. Letting any of
these reach the client as a 500 would turn a precise refusal into "something
broke".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from groundscribe.api import auth
from groundscribe.api.routes import router
from groundscribe.app.reads import UnknownArtefact
from groundscribe.app.rehydrate import MissingInput
from groundscribe.app.runtime import Runtime
from groundscribe.app.services import UnknownProject
from groundscribe.experiments.replay import NotRerunnable
from groundscribe.workflow.errors import (
    ArtifactProvenanceError,
    AttributionRequired,
    ExportMismatchError,
    HumanActionRequired,
    IllegalTransition,
    LineageError,
    SilentMutationError,
)

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
    # Something named does not exist.
    (UnknownProject, 404),
    (MissingInput, 404),
    (UnknownArtefact, 404),
    # Asked to repeat something that was never queued, or whose inputs are gone.
    # 409: the request is well formed and the *execution* cannot support it.
    (NotRerunnable, 409),
)


def create_app(*, runtime_factory: RuntimeFactory, password: str | None = None) -> FastAPI:
    """Build the API around a way of getting a runtime.

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
    app.state.password = password
    app.include_router(auth.router)
    app.include_router(router)

    if password is not None:
        app.middleware("http")(_require_session)

    for exception_type, status in _STATUS_FOR:
        app.add_exception_handler(exception_type, _handler(status))
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
    if request.url.path.startswith(auth.PUBLIC_PREFIX):
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


__all__ = ["DESCRIPTION", "TITLE", "VERSION", "RuntimeFactory", "create_app"]
