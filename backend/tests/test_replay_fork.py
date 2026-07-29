"""Inspect, replay, fork (phase 12).

plan/12 → *Inspect* displays exactly what happened; *replay* re-executes a stage
with recorded inputs and config, as a **new linked execution** because hosted
models are nondeterministic; *fork* starts from an existing execution and alters
one or more named variables, and is "the primary improvement mechanism".

Phase 09 built the endpoints and said outright that fork and replay were the same
thing until this phase gave them a difference. This is that difference, and it is
exactly one thing: a fork carries *variables*.

The reproducibility contract is what the tests are really guarding. We promise
complete inspection, preserved configuration, deterministic operations repeating,
replays linked to their original, and a comparison between them. We do not
promise that a hosted model says the same thing twice — so a replay that
overwrote its original, or a fork that quietly changed more than it was asked to,
would be a claim the system cannot support.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.provenance import models
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import SETTLED_GAPS, Walkthrough
from service_helpers import AUTHOR, Harness, build_harness

EXTRACTION = "extract_source_truth"


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


@pytest.fixture
def walk(client: TestClient, harness: Harness) -> Walkthrough:
    return Walkthrough(client, harness)


def script_again(walk: Walkthrough) -> None:
    """Script what a re-run of the extraction job will ask for.

    Both stages, because the job runs both: extraction continues into gap
    analysis, since there is no workflow state in between for a second job to be
    queued from. A replay repeats the *job*, so it repeats both — and the second
    pass hands back the same question labels the first did, which is the case
    that used to collide (phase 06's gap rows now keep their own id).
    """
    walk.script(EXTRACTION, walk.source_model())
    walk.script("generate_gap_questions", SETTLED_GAPS)


async def extracted(walk: Walkthrough) -> str:
    """A project with one finished extraction, and that execution's id."""
    await walk.open_project()
    await walk.extract()
    return walk.executions(EXTRACTION)[0]


def inspect(client: TestClient, execution_id: str) -> dict[str, Any]:
    response = client.get(f"/executions/{execution_id}/inspect")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def send(walk: Walkthrough, path: str, **body: Any) -> str:
    """Ask for a replay or a fork, let the worker do it, and say which execution it opened.

    The endpoints answer with a job, because re-running a stage calls a model and
    phase 09 keeps those out of the request. The execution exists once the worker
    has opened it, and the job is what names it.
    """
    response = walk.client.post(path, json=body)
    assert response.status_code == 202, response.text
    assert response.json()["source_execution_id"]

    (job, *rest) = await walk.harness.drain()
    assert not rest, "one re-run, one job"
    assert job.status.value == "succeeded", job.error_message
    assert job.stage_execution_id is not None
    return job.stage_execution_id


# ----------------------------------------------------------------------
# Inspect
# ----------------------------------------------------------------------


async def test_inspecting_shows_exactly_what_was_recorded(walk: Walkthrough) -> None:
    """plan/12 → *Inspect: display exactly what happened in the original run*.

    Compared against the rows rather than against a fixture: an inspector tested
    against expectations someone typed is an inspector that agrees with the
    person who wrote the test, not with the database.
    """
    execution_id = await extracted(walk)
    stored = walk.harness.runtime.session.get(models.StageExecution, execution_id)
    assert stored is not None

    inspection = inspect(walk.client, execution_id)

    assert inspection["summary"]["stage"] == stored.stage
    assert inspection["summary"]["status"] == stored.status.value
    assert [call["id"] for call in inspection["invocations"]] == [
        call.id for call in stored.model_invocations
    ]
    assert [event["sequence"] for event in inspection["events"]] == [
        event.sequence for event in stored.trace_events
    ]
    assert [artefact["snapshot_id"] for artefact in inspection["outputs"]] == [
        artefact.snapshot_id for artefact in stored.outputs
    ]


# ----------------------------------------------------------------------
# Replay
# ----------------------------------------------------------------------


async def test_a_replay_runs_the_stage_again_without_touching_the_original(
    walk: Walkthrough,
) -> None:
    """plan/12 → a replay is a *new linked execution*; the original is intact.

    Both halves matter. A replay that did not re-run would be an inspection with
    a misleading name, and one that overwrote its original would destroy the only
    thing worth comparing it against.
    """
    execution_id = await extracted(walk)
    before = inspect(walk.client, execution_id)

    script_again(walk)
    replayed_id = await send(walk, f"/executions/{execution_id}/replay", actor_id=AUTHOR)

    after = inspect(walk.client, execution_id)
    fresh = inspect(walk.client, replayed_id)

    assert fresh["summary"]["id"] != execution_id
    assert after == before, "the original must be exactly as it was"
    assert fresh["summary"]["stage"] == EXTRACTION
    assert fresh["invocations"], "a replay that called no model did not replay anything"
    assert fresh["outputs"], "it produced its own artefacts"


async def test_a_replay_keeps_the_configuration_it_replays(walk: Walkthrough) -> None:
    """plan/12 → *re-execute with recorded inputs + config*.

    The promise is config preservation, not identical prose: same prompt, same
    version, same model. What the model says is its own business.
    """
    execution_id = await extracted(walk)
    original = inspect(walk.client, execution_id)["invocations"][0]

    script_again(walk)
    replayed_id = await send(walk, f"/executions/{execution_id}/replay", actor_id=AUTHOR)
    repeated = inspect(walk.client, replayed_id)["invocations"][0]

    assert repeated["template_id"] == original["template_id"]
    assert repeated["template_version"] == original["template_version"]
    assert repeated["provider"] == original["provider"]
    assert repeated["model"] == original["model"]


# ----------------------------------------------------------------------
# Fork
# ----------------------------------------------------------------------


async def test_a_fork_changes_the_variable_it_was_given_and_nothing_else(
    walk: Walkthrough,
) -> None:
    """plan/12 → *fork … but alter one or more variables*; the rest is inherited."""
    execution_id = await extracted(walk)
    original = inspect(walk.client, execution_id)["invocations"][0]

    script_again(walk)
    forked_id = await send(
        walk,
        f"/executions/{execution_id}/fork",
        actor_id=AUTHOR,
        variables={"model": "llama3.1:8b-instruct"},
        reason="cheaper model, same prompt",
    )
    altered = inspect(walk.client, forked_id)["invocations"][0]

    assert altered["model"] == "llama3.1:8b-instruct"
    assert altered["model"] != original["model"]
    assert altered["template_id"] == original["template_id"]
    assert altered["template_version"] == original["template_version"]
    assert altered["provider"] == original["provider"]


async def test_a_fork_records_what_was_changed_and_who_asked(walk: Walkthrough) -> None:
    """An experiment nobody can attribute is an anecdote.

    The variables are written into a decision record on the fork, so the reason a
    later comparison shows a difference is answerable from the trace rather than
    from someone's memory.
    """
    execution_id = await extracted(walk)

    script_again(walk)
    forked_id = await send(
        walk,
        f"/executions/{execution_id}/fork",
        actor_id=AUTHOR,
        variables={"temperature": 0.9},
        reason="does it wander?",
    )

    decisions = inspect(walk.client, forked_id)["decisions"]
    (fork_decision,) = [item for item in decisions if item["decision_type"] == "execution_fork"]
    assert fork_decision["decided_by"] == AUTHOR
    assert fork_decision["inputs"]["variables"] == {"temperature": 0.9}
    assert fork_decision["inputs"]["source_execution_id"] == execution_id
    assert fork_decision["rationale"] == "does it wander?"


async def test_a_fork_with_no_variables_is_a_replay_and_says_so(walk: Walkthrough) -> None:
    """The two are the same operation with and without a change.

    Refusing an empty fork would be pedantry; pretending it is something other
    than a replay would be worse. It is recorded as what it is.
    """
    execution_id = await extracted(walk)

    script_again(walk)
    forked_id = await send(walk, f"/executions/{execution_id}/fork", actor_id=AUTHOR)

    decisions = inspect(walk.client, forked_id)["decisions"]
    assert [item["decision_type"] for item in decisions if "fork" in item["decision_type"]] == []
    assert [item for item in decisions if item["decision_type"] == "execution_replay"]


async def test_a_variable_the_system_cannot_change_is_refused(walk: Walkthrough) -> None:
    """A closed vocabulary, for the reason the trace filters are closed.

    An unrecognised variable that was quietly ignored would produce an experiment
    whose candidate configuration was never applied, and a result table saying
    the change made no difference.
    """
    execution_id = await extracted(walk)

    response = walk.client.post(
        f"/executions/{execution_id}/fork",
        json={"actor_id": AUTHOR, "variables": {"vibes": "better"}},
    )

    assert response.status_code == 422
    assert "vibes" in response.text


async def test_a_fork_nobody_asked_for_is_refused(walk: Walkthrough) -> None:
    """plan/03 refuses to store a decision nobody is accountable for."""
    execution_id = await extracted(walk)

    response = walk.client.post(
        f"/executions/{execution_id}/fork", json={"variables": {"temperature": 0.2}}
    )

    assert response.status_code == 422
