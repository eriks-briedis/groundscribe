"""Reaching the experimentation system from outside Python (phase 12).

plan/12's implementation task 7 → *surface comparisons in the phase-11
run-comparison UI*, and phase 09's rule that every capability reaches a person
through the same service layer from either interface.

Two things are being pinned.

**The experiment system is reachable.** A corpus that can only be built by
importing a module, and an experiment that can only be run from a Python shell,
is a deliverable in the same sense a design document is. The endpoints here are
the ones the run-comparison screen and the CLI both go through.

**The comparison says what it does not know.** plan/12's named risk is
misleading reproducibility claims, and the place a person is most likely to form
one is the screen showing two executions of the same stage side by side. So the
comparison carries the contract with it — including the refusal — rather than
leaving the reader to infer what the differences mean.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import Walkthrough
from service_helpers import AUTHOR, Harness, build_harness

VOICE = "align_voice"


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


@pytest.fixture
def walk(client: TestClient, harness: Harness) -> Walkthrough:
    return Walkthrough(client, harness)


async def approved(walk: Walkthrough) -> None:
    await walk.to_approval()
    await walk.command("POST", f"/articles/{walk.article_id}/approve", json={"actor_id": AUTHOR})


def post(walk: Walkthrough, path: str, **body: Any) -> dict[str, Any]:
    response = walk.client.post(path, json=body)
    assert response.status_code in (200, 201, 202), response.text
    payload: dict[str, Any] = response.json()
    return payload


def get(walk: Walkthrough, path: str, **params: Any) -> Any:
    response = walk.client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ----------------------------------------------------------------------
# The reproducibility contract
# ----------------------------------------------------------------------


async def test_the_reproducibility_contract_is_served_with_its_refusal(
    walk: Walkthrough,
) -> None:
    """plan/12's risk, answered where a person actually reads it.

    A contract kept in a README is a contract nobody consults while looking at
    two executions and deciding what their difference proves.
    """
    contract = get(walk, "/reproducibility")

    promised = [item for item in contract if item["promised"]]
    refused = [item for item in contract if not item["promised"]]
    assert len(promised) == 5
    assert refused
    assert "hosted" in refused[0]["detail"]


async def test_a_comparison_carries_the_contract_it_should_be_read_under(
    walk: Walkthrough,
) -> None:
    """The screen showing two runs of one stage is where the claim gets made.

    Two executions differing in their output is a fact; "the replay disagreed
    with the original" is an interpretation, and whether it is available depends
    on something this comparison has to say out loud.
    """
    await approved(walk)
    voice_id = walk.executions(VOICE)[0]
    walk.script(VOICE, walk.voice_pass(snapshot_id=walk.approved_input()))
    replay = post(walk, f"/executions/{voice_id}/replay", actor_id=AUTHOR)
    assert replay["source_execution_id"] == voice_id
    (job,) = await walk.harness.drain()

    comparison = get(walk, "/executions/compare", left=voice_id, right=job.stage_execution_id)

    assert comparison["differences"]
    assert [item["name"] for item in comparison["reproducibility"] if not item["promised"]] == [
        "identical_model_output"
    ]


# ----------------------------------------------------------------------
# Datasets and experiments
# ----------------------------------------------------------------------


async def test_a_dataset_is_built_and_read_back_over_http(walk: Walkthrough) -> None:
    """The corpus, as something a person can make without opening a shell."""
    await approved(walk)

    created = post(
        walk,
        "/evaluation-datasets",
        name="approved work",
        created_by=AUTHOR,
        description="everything published so far",
    )
    listed = get(walk, "/evaluation-datasets")

    assert created["entries"]
    assert created["created_by"] == AUTHOR
    assert [item["id"] for item in listed] == [created["id"]]


async def test_an_experiment_runs_its_arms_and_reports_the_comparison(
    walk: Walkthrough,
) -> None:
    """The whole loop over HTTP: open, start, let the worker run, read the table."""
    await approved(walk)
    dataset = post(walk, "/evaluation-datasets", name="corpus", created_by=AUTHOR)

    experiment = post(
        walk,
        "/experiments",
        name="cheaper model?",
        dataset_id=dataset["id"],
        created_by=AUTHOR,
        arms=[
            {"label": "baseline", "baseline": True},
            {"label": "small", "variables": {"model": "llama3.1:8b-instruct"}},
        ],
    )
    for _ in experiment["arms"]:
        walk.script(VOICE, walk.voice_pass(snapshot_id=walk.approved_input()))
    post(walk, f"/experiments/{experiment['id']}/start")
    await walk.harness.drain()

    read = get(walk, f"/experiments/{experiment['id']}")

    assert [row["label"] for row in read["comparison"]] == ["baseline", "small"]
    assert [row["baseline"] for row in read["comparison"]] == [True, False]
    assert len(read["results"]) == 2
    assert all(result["stage_execution_id"] for result in read["results"])


async def test_an_experiment_without_a_baseline_is_refused_over_http(
    walk: Walkthrough,
) -> None:
    """The same refusal the runner makes, with a status a client can act on."""
    await approved(walk)
    dataset = post(walk, "/evaluation-datasets", name="corpus", created_by=AUTHOR)

    response = walk.client.post(
        "/experiments",
        json={
            "name": "no control",
            "dataset_id": dataset["id"],
            "created_by": AUTHOR,
            "arms": [{"label": "candidate", "variables": {"temperature": 0.9}}],
        },
    )

    assert response.status_code == 422, response.text
    assert "baseline" in response.text


async def test_a_person_records_a_preference_over_http(walk: Walkthrough) -> None:
    """plan/12 → *human-preference decisions*, from the screen that shows both."""
    await approved(walk)
    dataset = post(walk, "/evaluation-datasets", name="corpus", created_by=AUTHOR)
    experiment = post(
        walk,
        "/experiments",
        name="cheaper model?",
        dataset_id=dataset["id"],
        created_by=AUTHOR,
        arms=[
            {"label": "baseline", "baseline": True},
            {"label": "small", "variables": {"model": "llama3.1:8b-instruct"}},
        ],
    )
    for _ in experiment["arms"]:
        walk.script(VOICE, walk.voice_pass(snapshot_id=walk.approved_input()))
    post(walk, f"/experiments/{experiment['id']}/start")
    await walk.harness.drain()

    candidate = next(arm for arm in experiment["arms"] if not arm["baseline"])
    post(
        walk,
        f"/experiments/{experiment['id']}/preferences",
        entry_id=dataset["entries"][0]["id"],
        arm_id=candidate["id"],
        decided_by=AUTHOR,
        reason="tighter opening",
    )

    read = get(walk, f"/experiments/{experiment['id']}")
    preferred = {row["label"]: row["human_preference"] for row in read["comparison"]}
    assert preferred == {"baseline": 0.0, "small": 1.0}
