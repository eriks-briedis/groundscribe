"""The CLI, and its parity with the API (phase 09).

Spec (plan/09 → Test-first specification): *a CLI command and its API counterpart
invoke the same service method with equivalent arguments; the CLI contains no
transition logic of its own*, and (Deliverables) *all delegating to the service
layer*.

Parity is asserted by putting the **same recording double** behind both
interfaces and comparing what each asked it to do. That is the only form of the
claim that cannot rot: two implementations tested separately can agree on every
assertion and still diverge, because each test only knows about its own side.

The second claim is checked structurally rather than behaviourally. "Contains no
transition logic" is a statement about what the module may know, so the test
looks at what it imports: a CLI that cannot see the workflow package cannot hold
an opinion about it.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from golden import golden_text
from groundscribe.api.app import create_app
from groundscribe.api.routes import get_service
from groundscribe.app.services import ApplicationService, CommandResult
from groundscribe.cli import main as cli
from groundscribe.provenance import models
from groundscribe.provenance.enums import ExecutionStatus
from groundscribe.workflow.states import WorkflowState
from service_helpers import AUTHOR
from stage_helpers import DEFAULT_CONSTRAINTS


@dataclass
class Call:
    """One request made of the service, as either interface made it."""

    method: str
    kwargs: dict[str, Any]


@dataclass
class RecordingService:
    """Stands in for the application service and remembers what it was asked.

    Every command returns the same canned result, because what is under test is
    the *request* each interface makes, not what comes back. A double that
    varied its answers would let a difference in rendering masquerade as a
    difference in behaviour.
    """

    calls: list[Call] = field(default_factory=list)

    def __getattr__(self, name: str) -> Any:
        """Answer like the real service, including whether the call is awaited.

        The method is looked up on :class:`ApplicationService` first, so a typo
        in a route or a command is an ``AttributeError`` here rather than a
        recorded call to something that does not exist — and so an async command
        gets something awaitable back, as it would in production.
        """
        real = getattr(ApplicationService, name, None)
        if name.startswith("_") or real is None:
            raise AttributeError(name)

        def record(*args: Any, **kwargs: Any) -> Any:
            positional = {"target": args[0]} if args else {}
            self.calls.append(Call(method=name, kwargs={**positional, **kwargs}))
            return _RETURNS.get(name, _RESULT)

        async def record_async(*args: Any, **kwargs: Any) -> Any:
            return record(*args, **kwargs)

        return record_async if inspect.iscoroutinefunction(real) else record

    @property
    def last(self) -> Call:
        """The last *command*, ignoring the transaction boundary around it."""
        return [call for call in self.calls if call.method not in _LIFECYCLE][-1]

    def methods(self) -> list[str]:
        return [call.method for call in self.calls]


#: Not commands: the unit-of-work boundary each interface puts around one.
_LIFECYCLE = frozenset({"commit", "rollback"})

_RESULT = CommandResult(
    project_id="p1",
    run_id="r1",
    state=WorkflowState.SOURCE_INGESTED,
    available_actions=("cancel",),
)

_EXECUTION = models.StageExecution(
    id="e1",
    pipeline_run_id="r1",
    stage="extract_source_truth",
    impl_version="1.1",
    ordinal=0,
    status=ExecutionStatus.SUCCEEDED,
    correlation_id="c1",
    started_at=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
)

#: What the double hands back for the few commands that do not answer with a
#: command envelope. Only the shape matters — every assertion here is about the
#: call that was made, never about the reply.
_RETURNS: dict[str, Any] = {
    "get_execution": _EXECUTION,
    "replay_execution": _EXECUTION,
    "fork_execution": _EXECUTION,
    "compare_executions": (_EXECUTION, _EXECUTION),
}


@pytest.fixture
def recorder() -> RecordingService:
    return RecordingService()


@pytest.fixture
def cli_runner(recorder: RecordingService, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setattr(cli, "service_factory", lambda: recorder)
    return CliRunner()


@pytest.fixture
def client(recorder: RecordingService) -> TestClient:
    app = create_app(runtime_factory=lambda: None)  # type: ignore[arg-type,return-value]
    app.dependency_overrides[get_service] = lambda: recorder
    return TestClient(app)


def run(cli_runner: CliRunner, *args: str) -> None:
    result = cli_runner.invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output


# ----------------------------------------------------------------------
# Parity
# ----------------------------------------------------------------------


def test_creating_a_project_asks_for_the_same_thing_either_way(
    cli_runner: CliRunner, client: TestClient, recorder: RecordingService, tmp_path: Path
) -> None:
    """plan/09 → *the same service method with equivalent arguments*."""
    run(
        cli_runner,
        "project",
        "create",
        "--title",
        "Read-through caching",
        "--author",
        AUTHOR,
        "--audience",
        DEFAULT_CONSTRAINTS.audience,
        "--platform",
        DEFAULT_CONSTRAINTS.platform,
        "--depth",
        DEFAULT_CONSTRAINTS.depth.value,
        "--provider",
        DEFAULT_CONSTRAINTS.allowed_providers[0],
    )
    from_cli = recorder.last

    client.post(
        "/projects",
        json={
            "title": "Read-through caching",
            "author_id": AUTHOR,
            "constraints": DEFAULT_CONSTRAINTS.model_dump(mode="json"),
        },
    )
    from_api = recorder.last

    assert from_cli.method == from_api.method == "create_project"
    assert from_cli.kwargs["title"] == from_api.kwargs["title"]
    assert from_cli.kwargs["author_id"] == from_api.kwargs["author_id"]
    assert from_cli.kwargs["constraints"].audience == from_api.kwargs["constraints"].audience
    assert (
        from_cli.kwargs["constraints"].allowed_providers
        == from_api.kwargs["constraints"].allowed_providers
    )


def test_importing_a_source_asks_for_the_same_thing_either_way(
    cli_runner: CliRunner, client: TestClient, recorder: RecordingService, tmp_path: Path
) -> None:
    """The CLI reads a file where the API takes a body; the service call matches."""
    source = tmp_path / "source.md"
    source.write_text(golden_text("source.md"), encoding="utf-8")

    run(cli_runner, "source", "import", "p1", "--title", "Caching", "--file", str(source))
    from_cli = recorder.last

    client.post(
        "/projects/p1/sources",
        json={"title": "Caching", "text": golden_text("source.md"), "source_format": "markdown"},
    )
    from_api = recorder.last

    assert from_cli.method == from_api.method == "import_source"
    assert from_cli.kwargs == from_api.kwargs


@pytest.mark.parametrize(
    ("command", "endpoint", "method"),
    [
        (("source", "extract", "p1"), "/projects/p1/source-model/extract", "extract_source_model"),
        (
            ("architecture", "propose", "p1"),
            "/projects/p1/architecture/propose",
            "propose_architecture",
        ),
        (("article", "draft", "a1"), "/articles/a1/draft", "draft"),
        (("article", "review", "a1"), "/articles/a1/review", "review"),
        (("article", "rewrite", "a1"), "/articles/a1/rewrite", "rewrite"),
    ],
)
def test_each_command_reaches_the_service_the_same_way_from_either_interface(
    cli_runner: CliRunner,
    client: TestClient,
    recorder: RecordingService,
    command: tuple[str, ...],
    endpoint: str,
    method: str,
) -> None:
    """The spec's command list, checked pair by pair."""
    run(cli_runner, *command)
    from_cli = recorder.last

    client.post(endpoint, json={})
    from_api = recorder.last

    assert from_cli.method == from_api.method == method
    assert from_cli.kwargs == from_api.kwargs


def test_a_human_action_carries_its_actor_from_either_interface(
    cli_runner: CliRunner, client: TestClient, recorder: RecordingService
) -> None:
    """Attribution is not something one interface may drop."""
    run(cli_runner, "architecture", "approve", "p1", "--by", AUTHOR)
    from_cli = recorder.last

    client.post("/projects/p1/architecture/current/approve", json={"actor_id": AUTHOR})
    from_api = recorder.last

    assert from_cli.method == from_api.method == "approve_architecture"
    assert from_cli.kwargs == from_api.kwargs == {"target": "p1", "approved_by": AUTHOR}


def test_inspecting_replaying_and_forking_match_their_endpoints(
    cli_runner: CliRunner, client: TestClient, recorder: RecordingService
) -> None:
    """plan/09 → ``execution inspect/replay/fork``."""
    run(cli_runner, "execution", "replay", "e1", "--by", AUTHOR)
    from_cli = recorder.last

    client.post("/executions/e1/replay", json={"actor_id": AUTHOR})
    from_api = recorder.last

    assert from_cli.method == from_api.method == "replay_execution"
    assert from_cli.kwargs == from_api.kwargs == {"target": "e1", "requested_by": AUTHOR}


def test_comparing_executions_matches_its_endpoint(
    cli_runner: CliRunner, client: TestClient, recorder: RecordingService
) -> None:
    """plan/09 → ``experiment compare``."""
    run(cli_runner, "experiment", "compare", "e1", "e2")
    from_cli = recorder.last

    client.get("/executions/compare", params={"left": "e1", "right": "e2"})
    from_api = recorder.last

    assert from_cli.method == from_api.method == "compare_executions"


# ----------------------------------------------------------------------
# The CLI holds no rules of its own
# ----------------------------------------------------------------------


def test_the_cli_ends_the_transaction_it_opened(
    cli_runner: CliRunner, recorder: RecordingService
) -> None:
    """A command that printed its result and committed nothing did nothing.

    The unit a person can re-run is the unit that must either have happened or
    not, so each invocation is one transaction. The API's half of this is
    asserted where it can be: against a real database, in the cross-cutting
    suite, where a second connection either sees the row or does not.
    """
    run(cli_runner, "article", "draft", "a1")

    assert recorder.methods() == ["draft", "commit"]


def test_the_cli_cannot_see_the_workflow_at_all() -> None:
    """plan/09 → *no duplicated workflow logic*, checked at the import boundary.

    A module that cannot reference the state machine cannot restate its rules,
    which is a stronger guarantee than any assertion about behaviour: it holds
    for the commands nobody thought to test.
    """
    source = Path(cli.__file__).read_text(encoding="utf-8")

    assert "groundscribe.workflow" not in source
    assert "WorkflowAction" not in source
    assert "WorkflowState" not in source


def test_every_command_group_the_plan_names_exists(cli_runner: CliRunner) -> None:
    """The spec's CLI surface: project, source, architecture, article, execution."""
    output = cli_runner.invoke(cli.app, ["--help"]).output

    for group in (
        "project",
        "source",
        "architecture",
        "article",
        "execution",
        "experiment",
        "voice",
    ):
        assert group in output
