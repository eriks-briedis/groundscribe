"""The stack a person actually installs (phase 14).

plan/14 → *Docker Compose stack: frontend (:3000), backend (:8000), worker
(separate process), database (PostgreSQL; SQLite option for lightweight local),
storage (local artefact directory)*, and *Single install path: Docker Compose + a
one-command install/bootstrap script*.

Split in two on purpose, because the two halves fail differently and cost
differently.

**The shape of the stack** is asserted from the committed files, on every run. A
service dropped from the compose file, a port that stopped matching the plan, a
worker folded back into the API process — each of those is a one-line mistake
that a person only discovers when the thing does not work on their machine, and
each is cheap to catch here.

**That it actually comes up** is asserted by running it, behind
``GROUNDSCRIBE_TEST_COMPOSE=1``. Building two images and starting four containers
is minutes, not seconds; making every test run pay that would mean the suite stops
being run. But a compose file nobody has ever executed is a document, not a
deployment, so the switch exists and CI can turn it on.

    GROUNDSCRIBE_TEST_COMPOSE=1 uv run pytest tests/test_deployment.py --no-cov
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
import yaml

from groundscribe.paths import repo_root

#: Turns on the half of this file that starts containers.
COMPOSE_ENV = "GROUNDSCRIBE_TEST_COMPOSE"

#: The ports plan/14 names. Written from the plan, not read from the file, so a
#: port quietly changed fails here rather than in somebody's browser.
FRONTEND_PORT = 3000
BACKEND_PORT = 8000

#: What the live stack is locked with. A fixed string because the test has to
#: know it; a real installation gets a generated one from `scripts/install.sh`.
PASSWORD = "compose-smoke-password"

ROOT = repo_root()


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    return document


# ----------------------------------------------------------------------
# The shape of the stack
# ----------------------------------------------------------------------


def test_the_stack_has_the_five_parts_the_plan_names(compose: dict[str, Any]) -> None:
    """plan/14 → frontend, backend, worker, database, storage."""
    services = compose["services"]

    assert {"frontend", "backend", "worker", "database"} <= set(services)
    # Storage is a volume rather than a service: an artefact directory is a place
    # to put bytes, and a container whose job is to hold a directory would be a
    # process with nothing to do.
    assert "storage" in compose["volumes"]


def test_the_ports_are_the_ones_the_plan_publishes(compose: dict[str, Any]) -> None:
    """A port that drifted is a broken bookmark and a wrong README at once."""
    services = compose["services"]

    assert any(str(FRONTEND_PORT) in str(port) for port in services["frontend"]["ports"])
    assert any(str(BACKEND_PORT) in str(port) for port in services["backend"]["ports"])


def test_the_worker_is_its_own_process_over_the_same_image(compose: dict[str, Any]) -> None:
    """plan/14 → *worker (separate process)*, and plan/09's reason for it.

    The same image as the backend, because they are the same application and two
    images would let the worker run code the API has not been given. A different
    *command*, because a worker that shared the API's process would be back to
    doing model calls inside a request — which is the seam phase 09 exists to
    keep.
    """
    services = compose["services"]

    assert services["worker"]["build"] == services["backend"]["build"]
    assert services["worker"]["command"] != services["backend"]["command"]
    assert "worker" in json.dumps(services["worker"]["command"])


def test_the_database_is_optional_so_sqlite_stays_the_light_way_in(
    compose: dict[str, Any],
) -> None:
    """plan/14 → *database (PostgreSQL; SQLite option for lightweight local)*.

    Postgres sits behind a compose profile, so ``docker compose up`` gives a
    person the SQLite stack — no server, no tuning, one fewer thing to have gone
    wrong on a first run — and ``--profile postgres`` gives them the concurrent
    one. The backend's dependency on it is ``required: false`` for the same
    reason: a stack that refused to start without a database it does not use
    would make the light path the harder one.
    """
    services = compose["services"]

    assert services["database"]["profiles"] == ["postgres"]
    assert services["backend"]["depends_on"]["database"]["required"] is False
    # And the default URL is a file on the shared volume, not a host nobody
    # started.
    assert "sqlite" in json.dumps(services["backend"]["environment"])


def test_every_service_that_holds_state_keeps_it_outside_the_container(
    compose: dict[str, Any],
) -> None:
    """Artefacts are the product. A stack that lost them on ``compose down`` would
    be a demonstration rather than an installation."""
    services = compose["services"]

    for name in ("backend", "worker"):
        mounted = [str(volume) for volume in services[name]["volumes"]]
        assert any("storage" in entry for entry in mounted), name

    assert "database" in compose["volumes"]


def test_the_images_the_stack_builds_from_are_committed(compose: dict[str, Any]) -> None:
    """A compose file naming a Dockerfile that is not there fails at build time
    with a message about paths rather than about what is missing."""
    services = compose["services"]

    for name in ("backend", "frontend"):
        build = services[name]["build"]
        dockerfile = ROOT / build["context"] / build["dockerfile"]
        assert dockerfile.is_file(), f"{name}: {dockerfile} is missing"


# ----------------------------------------------------------------------
# The way in
# ----------------------------------------------------------------------


def test_there_is_one_command_that_installs_this() -> None:
    """plan/14 → *a one-command install/bootstrap script*.

    Executable, and it refuses rather than guesses: an install script that
    silently did half the job leaves a person debugging a stack that was never
    fully built.
    """
    script = ROOT / "scripts" / "install.sh"

    assert script.is_file()
    assert os.access(script, os.X_OK), "the install script is not executable"
    text = script.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text, "a bootstrap that continues past a failure is worse"
    assert "--postgres" in text, "the Postgres path has to be reachable from the one command"


def test_the_install_script_explains_itself_without_doing_anything() -> None:
    """``--help`` is the first thing a person types at an unfamiliar script, and
    it must not be the thing that starts four containers."""
    result = subprocess.run(
        [str(ROOT / "scripts" / "install.sh"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "postgres" in result.stdout.lower()
    assert "compose" in result.stdout.lower()


# ----------------------------------------------------------------------
# That it comes up
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def stack() -> Iterator[str]:
    """The real stack, built and started, torn down afterwards.

    Bound to a port the kernel picked rather than to 8000. The published port is
    a *host* concern — inside the network the API is always on 8000, which is
    what the frontend proxies to — and a test that insisted on the well-known
    port would fail on any machine already running something there, including
    this one. Nothing about the deployment is being changed to suit the test:
    `GROUNDSCRIBE_API_PORT` is the same knob the README hands a person whose
    port is taken.
    """
    if not os.environ.get(COMPOSE_ENV):
        pytest.skip(f"{COMPOSE_ENV} is not set: not building images in this run")
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")

    project = "groundscribe-test-stack"
    port = _free_port()
    env = {
        **os.environ,
        "GROUNDSCRIBE_PASSWORD": PASSWORD,
        "GROUNDSCRIBE_API_PORT": str(port),
    }
    compose_project = ["docker", "compose", "-p", project]
    up = subprocess.run(
        [*compose_project, "up", "-d", "--build", "backend", "worker"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=2400,
    )
    try:
        assert up.returncode == 0, up.stderr
        yield f"http://127.0.0.1:{port}"
    finally:
        subprocess.run(
            [*compose_project, "down", "-v"],
            cwd=ROOT,
            capture_output=True,
            timeout=300,
            env=env,
        )


def test_the_api_answers_and_a_command_round_trips(stack: str) -> None:
    """plan/14 → *`compose up` brings the stack healthy; a basic command
    round-trips*.

    The one test here that proves the software can be *installed* rather than
    merely imported: it builds the image the README tells a person to build and
    talks to the result over a socket.

    Health, then a real write, then reading it back. A health check alone would
    pass against an API with no database behind it — which, given the entrypoint
    runs the migrations, is the failure this is most likely to be catching.
    """
    session = _signed_in(stack)

    created = _post(
        session,
        f"{stack}/projects",
        {
            "title": "Installed from compose",
            "author_id": "ada",
            "constraints": {
                "audience": "engineers",
                "platform": "blog",
                "depth": "practitioner",
                "target_length_words": 400,
            },
        },
    )

    assert created["state"] == "source_ingested"
    assert created["project_id"] in json.dumps(_get(session, f"{stack}/projects"))


def test_the_health_check_needs_no_credential(stack: str) -> None:
    """A container orchestrator has no password and never will.

    A health check behind the session guard would report the stack unhealthy for
    exactly as long as the password was wrong, and restart it forever — which is
    backwards. It answers liveness and nothing else, so an unauthenticated caller
    learns only what the open socket already told them.
    """
    anonymous = urllib.request.build_opener()

    health = _get(anonymous, f"{stack}/health")

    assert health["status"] == "ok"
    with pytest.raises(urllib.error.HTTPError) as refused:
        _get(anonymous, f"{stack}/projects")
    assert refused.value.code == 401


def test_the_worker_is_running_beside_the_api(stack: str) -> None:
    """A stack whose worker exited looks perfectly healthy right up until the
    first command queues a job nobody drains."""
    running = subprocess.run(
        ["docker", "compose", "-p", "groundscribe-test-stack", "ps", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert running.returncode == 0, running.stderr
    states = {
        entry["Service"]: entry["State"]
        for entry in (json.loads(line) for line in running.stdout.splitlines() if line)
    }
    assert states.get("worker") == "running", states


def _free_port() -> int:
    """A port the kernel says is free, asked for by binding one and letting go."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _signed_in(base: str) -> urllib.request.OpenerDirector:
    """An opener holding the session cookie, once the API is answering.

    Cookies rather than a bearer token because that is what phase 13 built: the
    session is `HttpOnly` and `SameSite=Lax`, so a client that wanted a header
    would be describing a different application.
    """
    _await_http(f"{base}/health", timeout=240)
    session = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    _post(session, f"{base}/auth/login", {"password": PASSWORD}, expect_body=False)
    return session


def _await_http(url: str, *, timeout: float) -> None:
    """Poll until the service answers, or say how long it did not."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
        time.sleep(2)
    raise AssertionError(f"{url} never answered within {timeout}s (last: {last})")


def _request(
    session: urllib.request.OpenerDirector,
    url: str,
    *,
    data: bytes | None,
    expect_body: bool = True,
) -> Any:
    request = urllib.request.Request(url, data=data)
    request.add_header("content-type", "application/json")
    with session.open(request, timeout=60) as response:
        body = response.read().decode()
    return json.loads(body) if expect_body and body else None


def _post(
    session: urllib.request.OpenerDirector,
    url: str,
    body: dict[str, Any],
    *,
    expect_body: bool = True,
) -> Any:
    return _request(session, url, data=json.dumps(body).encode(), expect_body=expect_body)


def _get(session: urllib.request.OpenerDirector, url: str) -> Any:
    return _request(session, url, data=None)
