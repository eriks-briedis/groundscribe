"""Evaluation datasets, built from work a person actually approved (phase 12).

plan/12 → *Evaluation datasets: built from approved historical runs; entries
reference immutable snapshots (not mutable project state); sensitive projects
excluded unless explicitly approved*, tested as *mutating project state
afterward does not change the dataset*.

Three constraints, each answering a way an evaluation set goes wrong.

**Approved, not merely finished.** A run that reached the end is not evidence of
anything; a run a person put their name to is. Scoring candidate configurations
against articles nobody was willing to publish would measure agreement with a
draft, which is the thing being tested.

**Snapshots, not project state.** A dataset entry naming an *article* would
change every time that article was revised, and the same experiment would
silently stop being the same experiment. Entries name the immutable snapshot and
the execution that produced it, which is also what makes an arm runnable: an
experiment forks that execution.

**Sensitive material excluded until somebody says otherwise.** Confidential
source stays out of a corpus that will be re-run under configurations nobody has
reviewed, and a project that never consented to its trace being kept has
certainly not consented to being a benchmark. Inclusion is a decision, and it is
recorded as one.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.experiments.datasets import DatasetBuilder, SensitiveProject
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import Walkthrough
from service_helpers import AUTHOR, Harness, build_harness


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


@pytest.fixture
def walk(client: TestClient, harness: Harness) -> Walkthrough:
    return Walkthrough(client, harness)


@pytest.fixture
def builder(harness: Harness) -> DatasetBuilder:
    return DatasetBuilder(harness.runtime.session, snapshots=harness.runtime.snapshots)


async def approved(walk: Walkthrough, *, confidential: bool = False) -> str:
    """Walk one project all the way to a person approving the article."""
    await walk.open_project(confidential=confidential)
    await walk.extract()
    await walk.architecture()
    await walk.brief()
    await walk.draft()
    await walk.review()
    await walk.revise()
    await walk.review(clean=True)
    await walk.align_voice()
    await walk.score()
    await walk.validate()
    await walk.command("POST", f"/articles/{walk.article_id}/approve", json={"actor_id": AUTHOR})
    return walk.project_id


async def test_a_run_nobody_approved_contributes_nothing(
    walk: Walkthrough, builder: DatasetBuilder
) -> None:
    """Approved, not merely finished.

    The walk stops one command short — validated, waiting on a person — which is
    the state most runs in a real database are in. Treating it as evidence would
    fill a benchmark with articles their author declined to publish.
    """
    await walk.to_approval()

    dataset = builder.build(name="unapproved", created_by=AUTHOR)

    assert dataset.entries == []


async def test_an_approved_run_becomes_one_entry(
    walk: Walkthrough, builder: DatasetBuilder
) -> None:
    """And the entry names the execution an experiment will fork.

    plan/12 calls fork the primary improvement mechanism, so a dataset entry that
    could not be forked would be a corpus with no way to run anything against it.
    """
    project_id = await approved(walk)

    dataset = builder.build(name="approved work", created_by=AUTHOR)

    (entry,) = dataset.entries
    assert entry.project_id == project_id
    assert entry.stage_execution_id
    assert entry.reference_snapshot_id
    assert entry.ordinal == 0


async def test_an_entry_does_not_change_when_the_project_does(
    walk: Walkthrough, builder: DatasetBuilder
) -> None:
    """plan/12 → *mutating project state afterward does not change the dataset*.

    The article is sent back and rewritten after the dataset was built. Its
    newest version is now different prose; the entry still names, and still
    reads back, what was approved.
    """
    await approved(walk)
    dataset = builder.build(name="before", created_by=AUTHOR)
    (entry,) = dataset.entries
    approved_body = builder.reference(entry)["body"]

    # The article moves on: the voice pass is run again, which writes a new
    # version branching from the one it edited last time.
    voice_id = walk.executions("align_voice")[0]
    walk.script(
        "align_voice", walk.voice_pass(snapshot_id=walk.input_snapshot(voice_id, "article_version"))
    )
    response = walk.client.post(f"/executions/{voice_id}/fork", json={"actor_id": AUTHOR})
    assert response.status_code == 202, response.text
    await walk.harness.drain()

    assert builder.reference(entry)["body"] == approved_body
    newest = walk.latest_version_snapshot()
    assert newest is not None
    assert json.loads(walk.harness.runtime.snapshots.read(newest))["body"] != approved_body


async def test_a_confidential_project_is_left_out(
    walk: Walkthrough, builder: DatasetBuilder
) -> None:
    """Confidential source material stays out of a corpus that will be re-run.

    Silently: the dataset is built, it simply does not contain it. Refusing the
    whole build would make one sensitive project block every other project's
    evidence, which is how a safety rule gets switched off.
    """
    await approved(walk, confidential=True)

    dataset = builder.build(name="quiet", created_by=AUTHOR)

    assert dataset.entries == []
    (candidate,) = builder.candidates()
    assert candidate.sensitive
    assert candidate.reason


async def test_a_confidential_project_may_be_included_by_name(
    walk: Walkthrough, builder: DatasetBuilder
) -> None:
    """plan/12 → *excluded unless explicitly approved*.

    By name, never by a flag meaning "all of them". A blanket switch is a
    decision made once, in a hurry, about projects that did not exist yet.
    """
    project_id = await approved(walk, confidential=True)

    dataset = builder.build(name="with consent", created_by=AUTHOR, include_sensitive=(project_id,))

    (entry,) = dataset.entries
    assert entry.project_id == project_id
    assert dataset.sensitive_included == [project_id]


async def test_including_a_sensitive_project_nobody_named_is_refused(
    walk: Walkthrough, builder: DatasetBuilder
) -> None:
    """Naming a project that is not sensitive is a mistake worth reporting.

    It means whoever built the dataset believed they were making an exception
    they were not making — and next time, when it matters, the same belief will
    be wrong in the other direction.
    """
    await approved(walk)

    with pytest.raises(SensitiveProject):
        builder.build(name="confused", created_by=AUTHOR, include_sensitive=("no-such-project",))


async def test_a_dataset_records_who_built_it_and_when(
    walk: Walkthrough, builder: DatasetBuilder
) -> None:
    """A benchmark nobody can attribute is a benchmark nobody can question."""
    await approved(walk)

    dataset = builder.build(name="attributed", created_by=AUTHOR, description="the first cut")

    assert dataset.created_by == AUTHOR
    assert dataset.description == "the first cut"
    assert dataset.created_at is not None
