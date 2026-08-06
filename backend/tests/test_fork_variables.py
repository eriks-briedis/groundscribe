"""The variables a fork declares, and whether they actually do anything (phase 12).

plan/12 → *Fork: start from an existing execution but alter one or more variables
(prompt version, model, temperature, voice profile, rubric, source model,
context-selection strategy, revision plan) — the primary improvement mechanism*,
and its test: *forking changes only the specified variable(s); everything else is
inherited from the source execution and captured*.

The vocabulary was declared when fork was built and only four of its nine members
were wired to anything: prompt version, model, provider and temperature. The
other five were accepted, recorded in the fork's decision record, and then
ignored. ``variables.py`` had already written down why that is the worst
available outcome — *an experiment whose candidate configuration was silently
dropped does not fail, it succeeds, and reports that the change made no
difference* — so this closes the gap and then guards it.

Two rules come out of that, and both are tested here rather than trusted:

**Every declared variable is honoured by some stage.** A member of the vocabulary
that no stage reads is a lie the API tells about what it can do.

**A stage refuses a variable it cannot honour.** Forking an extraction with a
rubric version is not a harmless no-op; it is an experiment that will report the
rubric made no difference to a stage that has never had one.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.domain.enums import ArtifactType
from groundscribe.experiments.replay import STAGE_VARIABLES
from groundscribe.experiments.variables import ForkVariable
from groundscribe.jobs.models import Job
from groundscribe.paths import CONFIG_ROOT_ENV, config_root
from groundscribe.provenance import models
from groundscribe.scoring.rubric import (
    SCORING_RUBRIC_FILENAME,
    ScoringRubricError,
    default_scoring_rubric,
    scoring_rubric,
)
from groundscribe.stages.context import ContextStrategy
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import SETTLED_GAPS, Walkthrough
from service_helpers import AUTHOR, Harness, build_harness

EXTRACTION = "extract_source_truth"
ARCHITECTURE = "propose_content_architecture"
PLAN = "create_revision_plan"
REWRITE = "rewrite_substantively"
VOICE = "align_voice"
SCORING = "score_article"

#: What the installation actually ships, read rather than pinned. It moves
#: whenever an editorial judgement in the rubric does — it went to "2" when the
#: floors became per content type — and a test asserting the old number would
#: fail on a change it has no opinion about.
SHIPPED_RUBRIC_VERSION = default_scoring_rubric().version

#: A rubric version that is not the shipped one, written into a throwaway config
#: root. A second rubric shipped in ``config/`` would be a product decision — an
#: editorial judgement nobody asked for — where what is needed is only something
#: for an experiment to point at.
#:
#: Derived from the shipped version so it cannot collide with it. It was the
#: literal "2", which stopped being an alternative the day the shipped rubric
#: became version 2 — a fork onto "the other rubric" that pointed at the same one.
SECOND_RUBRIC_VERSION = f"{SHIPPED_RUBRIC_VERSION}-alternative"

#: The version string of the profile a voice fork points at. Distinctive because
#: the assertion is that the stage ran under *this* one rather than the one the
#: project resolves to by default.
CANDIDATE_VOICE = "candidate-1"


@pytest.fixture
def config_root_with_two_rubrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shipped config, plus one alternative rubric version to fork onto."""
    root = tmp_path / "config"
    shutil.copytree(config_root(), root)
    shipped = (root / SCORING_RUBRIC_FILENAME).read_text(encoding="utf-8")
    (root / f"scoring-rubric-{SECOND_RUBRIC_VERSION}.yaml").write_text(
        shipped.replace(
            f'version: "{SHIPPED_RUBRIC_VERSION}"', f'version: "{SECOND_RUBRIC_VERSION}"'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_ROOT_ENV, str(root))
    return root


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


@pytest.fixture
def walk(client: TestClient, harness: Harness) -> Walkthrough:
    return Walkthrough(client, harness)


async def fork(walk: Walkthrough, execution_id: str, **variables: Any) -> Job:
    """Fork one execution and let the worker run it, returning the finished job.

    The job rather than the execution id, because a stage reports what it ran
    under in its result — which profile version, which budget — and half these
    tests are about exactly that.
    """
    response = walk.client.post(
        f"/executions/{execution_id}/fork",
        json={"actor_id": AUTHOR, "variables": variables, "reason": "phase 12 experiment"},
    )
    assert response.status_code == 202, response.text

    (job, *rest) = await walk.harness.drain()
    assert not rest, "one fork, one job"
    assert job.status.value == "succeeded", job.error_message
    assert job.stage_execution_id is not None
    return job


def opened(job: Job) -> str:
    assert job.stage_execution_id is not None
    return job.stage_execution_id


def execution(walk: Walkthrough, execution_id: str) -> models.StageExecution:
    stored = walk.session.get(models.StageExecution, execution_id)
    assert stored is not None
    return stored


def inputs_of(walk: Walkthrough, execution_id: str, artifact_type: ArtifactType) -> list[str]:
    """The snapshot ids of one kind that an execution was run against."""
    return [
        artefact.snapshot_id
        for artefact in execution(walk, execution_id).inputs
        if artefact.snapshot is not None and artefact.snapshot.artifact_type is artifact_type
    ]


def outputs_of(walk: Walkthrough, execution_id: str, artifact_type: ArtifactType) -> list[str]:
    return [
        artefact.snapshot_id
        for artefact in execution(walk, execution_id).outputs
        if artefact.snapshot is not None and artefact.snapshot.artifact_type is artifact_type
    ]


# ----------------------------------------------------------------------
# The vocabulary, and what happens to a variable a stage cannot use
# ----------------------------------------------------------------------


def test_every_variable_the_vocabulary_declares_is_honoured_by_some_stage() -> None:
    """A fork variable no stage reads is a promise the API cannot keep.

    Written as a set difference rather than a per-variable assertion so that
    adding a tenth variable fails here, naming it, rather than passing quietly
    until somebody runs an experiment with it.
    """
    honoured = {variable for variables in STAGE_VARIABLES.values() for variable in variables}

    assert set(ForkVariable) - honoured == set()


async def test_a_variable_the_stage_cannot_honour_is_refused(walk: Walkthrough) -> None:
    """Not a no-op: an experiment that reports a rubric changed nothing.

    Refused where the request is made, so the person who asked finds out, rather
    than three layers down where the only honest thing left is to fail a job
    nobody is watching.
    """
    await walk.open_project()
    await walk.extract()
    execution_id = walk.executions(EXTRACTION)[0]

    response = walk.client.post(
        f"/executions/{execution_id}/fork",
        json={"actor_id": AUTHOR, "variables": {"rubric_version": SECOND_RUBRIC_VERSION}},
    )

    assert response.status_code == 409, response.text
    assert "rubric_version" in response.text
    assert EXTRACTION in response.text


async def test_a_context_strategy_the_system_does_not_have_is_refused(walk: Walkthrough) -> None:
    """The closed vocabulary again, one level down.

    ``context_strategy`` takes the name of a selection strategy, and a name
    nothing implements has to be refused for the same reason an unknown variable
    is: a run that silently fell back to the default would report that retrieval
    made no difference.
    """
    await walk.open_project()
    await walk.extract()
    execution_id = walk.executions(EXTRACTION)[0]

    response = walk.client.post(
        f"/executions/{execution_id}/fork",
        json={"actor_id": AUTHOR, "variables": {"context_strategy": "vibes_based"}},
    )

    assert response.status_code == 422, response.text
    assert "vibes_based" in response.text


# ----------------------------------------------------------------------
# Each variable, doing what it says
# ----------------------------------------------------------------------


async def test_forking_the_context_strategy_selects_the_source_differently(
    walk: Walkthrough,
) -> None:
    """plan/12 → *context-selection strategy* as a fork variable.

    The original keeps the strategy it ran under, which is the half that makes
    the pair comparable: two executions differing in exactly one recorded field.
    """
    await walk.open_project()
    await walk.extract()
    original_id = walk.executions(EXTRACTION)[0]

    walk.script(EXTRACTION, walk.source_model())
    walk.script("generate_gap_questions", SETTLED_GAPS)
    forked_id = opened(
        await fork(walk, original_id, context_strategy=ContextStrategy.RELEVANCE_RANKED.value)
    )

    (original,) = execution(walk, original_id).context_selections
    (forked,) = execution(walk, forked_id).context_selections
    assert original.strategy == ContextStrategy.IN_ORDER.value
    assert forked.strategy == ContextStrategy.RELEVANCE_RANKED.value


async def test_forking_the_source_model_runs_against_the_version_it_names(
    walk: Walkthrough,
) -> None:
    """plan/12 → *source model* as a fork variable.

    Answering a question rebuilds the source model, so the run has two of them.
    Re-proposing the architecture against the *earlier* one is a real question —
    did the answer change the shape of the article? — and it is unanswerable
    unless a fork can name which source model to read.
    """
    await walk.open_project()
    await walk.extract(blocking=True)
    await walk.answer()
    first, second = walk.snapshots(ArtifactType.SOURCE_MODEL)[:2]
    assert first != second

    await walk.architecture()
    original_id = walk.executions(ARCHITECTURE)[0]
    assert inputs_of(walk, original_id, ArtifactType.SOURCE_MODEL) == [second]

    walk.script(ARCHITECTURE, walk.architecture_payload())
    forked_id = opened(await fork(walk, original_id, source_model=first))

    assert inputs_of(walk, forked_id, ArtifactType.SOURCE_MODEL) == [first]


async def test_forking_the_revision_plan_rewrites_under_the_plan_it_names(
    walk: Walkthrough,
) -> None:
    """plan/12 → *revision plan* as a fork variable.

    Two forks, which is what makes this the improvement mechanism the plan calls
    it: replay the planning stage to get a second plan, then rewrite against
    that one instead. Neither the original plan nor the original rewrite moves.
    """
    await walk.to_approval()
    plan_id = walk.executions(PLAN)[0]
    rewrite_id = walk.executions(REWRITE)[0]
    original_plan = outputs_of(walk, plan_id, ArtifactType.REVISION_PLAN)[0]

    walk.script(PLAN, walk.revision_plan_payload())
    replanned_id = opened(await fork(walk, plan_id))
    second_plan = outputs_of(walk, replanned_id, ArtifactType.REVISION_PLAN)[0]
    assert second_plan != original_plan

    walk.script(REWRITE, walk.rewrite_payload())
    forked_id = opened(await fork(walk, rewrite_id, revision_plan=second_plan))

    assert inputs_of(walk, rewrite_id, ArtifactType.REVISION_PLAN) == [original_plan]
    assert inputs_of(walk, forked_id, ArtifactType.REVISION_PLAN) == [second_plan]


async def test_forking_the_voice_profile_writes_under_the_profile_it_names(
    walk: Walkthrough,
) -> None:
    """plan/12 → *voice profile* as a fork variable.

    A fork names a profile *version*, not a profile. The version is the immutable
    unit an article can cite; naming the profile would mean the experiment ran
    under whatever happened to be active when the worker got to it.
    """
    await walk.to_approval()
    voice_id = walk.executions(VOICE)[0]
    original = execution(walk, voice_id)
    version_id = walk.save_voice_profile(version=CANDIDATE_VOICE)

    walk.script(VOICE, walk.voice_pass())
    job = await fork(walk, voice_id, voice_profile=version_id)

    assert job.result["voice_profile_version"] == CANDIDATE_VOICE
    assert original.id != opened(job)


async def test_forking_the_rubric_scores_under_the_version_it_names(
    walk: Walkthrough, config_root_with_two_rubrics: Path
) -> None:
    """plan/12 → *rubric* as a fork variable.

    The evaluation record names the rubric version it was made under, which is
    what makes a score comparable to another score at all. A fork that changed
    the rubric without changing that field would produce two numbers nobody
    could tell apart.
    """
    await walk.to_approval()
    scoring_id = walk.executions(SCORING)[0]
    (original,) = execution(walk, scoring_id).evaluation_runs
    assert original.rubric_version == SHIPPED_RUBRIC_VERSION

    walk.script(SCORING, walk.score_payload())
    forked_id = opened(await fork(walk, scoring_id, rubric_version=SECOND_RUBRIC_VERSION))

    (rescored,) = execution(walk, forked_id).evaluation_runs
    assert rescored.rubric_version == SECOND_RUBRIC_VERSION


# ----------------------------------------------------------------------
# Loading a rubric by version
# ----------------------------------------------------------------------


def test_a_rubric_is_loadable_by_the_version_it_declares(
    config_root_with_two_rubrics: Path,
) -> None:
    """Versions are files, as prompts are, for the reason prompts are.

    A rubric that scored an article has to stay readable after a newer one
    replaces it, or every historical score becomes a number under a document
    that no longer exists.
    """
    assert scoring_rubric().version == SHIPPED_RUBRIC_VERSION
    assert scoring_rubric(SHIPPED_RUBRIC_VERSION).version == SHIPPED_RUBRIC_VERSION
    assert scoring_rubric(SECOND_RUBRIC_VERSION).version == SECOND_RUBRIC_VERSION


def test_a_rubric_version_nobody_wrote_is_refused(config_root_with_two_rubrics: Path) -> None:
    """Loudly, and naming where it looked.

    Falling back to the shipped rubric would score the candidate arm of an
    experiment under the baseline's rubric and report the two as comparable.
    """
    with pytest.raises(ScoringRubricError) as caught:
        scoring_rubric("47")

    assert "47" in str(caught.value)


def test_a_rubric_file_that_declares_a_different_version_is_refused(
    config_root_with_two_rubrics: Path,
) -> None:
    """The filename is a claim about the contents, and it is checked.

    A file named for version 2 that declares version 3 would be recorded against
    every score as whichever of the two the reader happened to trust.
    """
    path = config_root_with_two_rubrics / f"scoring-rubric-{SECOND_RUBRIC_VERSION}.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f'version: "{SECOND_RUBRIC_VERSION}"', 'version: "3"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ScoringRubricError) as caught:
        scoring_rubric(SECOND_RUBRIC_VERSION)

    assert "3" in str(caught.value)
