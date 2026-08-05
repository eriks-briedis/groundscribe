"""A project chooses which routing policy its stages run against (phase 15).

Routing was one file for the installation. That is the right default — a machine
has one set of models on it — but it makes "move this project to OpenAI" mean
"move every project to OpenAI", and the situation that forces the question is
per-project by nature: one source too long for the local model's context window,
in a project whose neighbours are fine.

What is under test here is the *selection*, not the routes. Which model a stage
should use is argued in the YAML and checked by ``writer llm probe``; these tests
ask whether a project's choice reaches the call, whether it survives the two
gates that were already there, and whether a wrong choice fails where the person
who made it can see it.

The three statements are deliberately independent, and each test that touches
more than one says which:

- a **key or an address** on the machine makes a provider reachable (bootstrap),
- a project's **allowed_providers** makes it permitted (phase 13),
- a project's **routing profile** decides where the calls go (here).

Selecting a profile is not consent, and consenting is not configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.app.services import generator_for
from groundscribe.domain import models as domain_models
from groundscribe.llm.routing import (
    ROUTING_CONFIG_FILENAME,
    ModelChoice,
    RoutingConfigError,
    StageRoute,
    available_profiles,
    default_routing_policy,
    profile_path,
    routing_policy,
)
from groundscribe.paths import CONFIG_ROOT_ENV, config_root
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.errors import AttributionRequired
from service_helpers import AUTHOR, Harness, build_harness
from stage_helpers import DEFAULT_CONSTRAINTS

#: The profile this repository actually ships, used where the point is that it
#: ships one. Written out rather than taken from ``available_profiles()`` — a
#: test that read the name it was asserting would pass against an empty config
#: directory.
SHIPPED_PROFILE = "openai"

#: The shipped default policy, read at import.
#:
#: Tests that redirect the config root need a *valid* default file inside it —
#: several of them assert that a project can be moved back onto the default, and
#: "back" has to lead somewhere. Read here rather than in the helper because by
#: the time the helper runs, ``config_root()`` is the temporary directory being
#: populated, and reading it there reads the file it is about to write.
SHIPPED_DEFAULT_TEXT = (config_root() / ROUTING_CONFIG_FILENAME).read_text(encoding="utf-8")

#: The stages the shipped policy routes, read at import for the same reason.
SHIPPED_STAGES = tuple(sorted(default_routing_policy().stages))


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


def new_project(harness: Harness) -> str:
    created = harness.service.create_project(
        title="Read-through caching",
        author_id=AUTHOR,
        constraints=DEFAULT_CONSTRAINTS,
    )
    return created.project_id


def write_profile(root: Path, name: str, *, provider: str, model: str) -> None:
    """A valid policy file, named as a profile of ``root``.

    Carries the *shipped* stage list rather than a default alone. A policy with
    no stages is legal — everything falls to the default route — but it is not
    the shape anything real has, and the visibility surface iterates stages, so a
    stageless fixture would let a screen that reported nothing pass a test about
    what it reports.
    """
    stages = "\n".join(
        f"  {stage}:\n    primary:\n      provider: {provider}\n      model: {model}"
        for stage in SHIPPED_STAGES
    )
    (root / f"model-routing.{name}.yaml").write_text(
        f'version: "test-{name}"\n'
        "default:\n"
        "  primary:\n"
        f"    provider: {provider}\n"
        f"    model: {model}\n"
        f"stages:\n{stages}\n",
        encoding="utf-8",
    )


# ----------------------------------------------------------------------
# Resolving a profile to a file
# ----------------------------------------------------------------------


class TestProfileResolution:
    def test_no_profile_is_the_shipped_default(self, tmp_path: Path) -> None:
        """``None`` is a real answer, not a missing one."""
        assert profile_path(None, root=tmp_path) == tmp_path / ROUTING_CONFIG_FILENAME

    def test_a_profile_is_a_sibling_of_the_default_not_a_child(self, tmp_path: Path) -> None:
        """Alternatives of equal standing, which is what they are."""
        assert profile_path("openai", root=tmp_path) == tmp_path / "model-routing.openai.yaml"

    def test_the_default_is_not_offered_as_a_profile(self, tmp_path: Path) -> None:
        """Listing it would make "default" and "openai" look like the same kind of answer.

        One of them is a choice; the other is what not choosing means, and on this
        installation it happens to be Ollama. A list that flattened the two would
        invite somebody to "select the default" and expect that to pin it.
        """
        (tmp_path / ROUTING_CONFIG_FILENAME).write_text("version: '1'\n", encoding="utf-8")
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")

        assert available_profiles(root=tmp_path) == ("openai",)

    def test_profiles_are_discovered_by_listing_not_by_registration(self, tmp_path: Path) -> None:
        """Adding a profile is adding a file, as with the policies themselves."""
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        write_profile(tmp_path, "groq", provider="groq", model="llama-4")

        assert available_profiles(root=tmp_path) == ("groq", "openai")

    def test_a_config_directory_that_is_not_there_offers_nothing(self, tmp_path: Path) -> None:
        """Rather than raising: "what can I choose" has an answer here, and it is none."""
        assert available_profiles(root=tmp_path / "absent") == ()


class TestProfileNames:
    """A profile name arrives from a person and becomes a path."""

    @pytest.mark.parametrize(
        "name",
        [
            "../../../../etc/passwd",
            "..",
            "/etc/passwd",
            "openai/../../secrets",
            "model-routing.yaml",
            "Openai",
            "open ai",
            "",
            "-openai",
        ],
    )
    def test_a_name_that_is_not_a_name_is_refused(self, name: str, tmp_path: Path) -> None:
        """Validated as a name, never sanitised as a path.

        The difference matters: stripping ``../`` would silently accept a
        corrected version of what was asked for, and the caller would be told it
        got what it sent.
        """
        with pytest.raises(RoutingConfigError, match="invalid routing profile"):
            profile_path(name, root=tmp_path)

    def test_the_refusal_names_what_is_allowed(self, tmp_path: Path) -> None:
        """A rejection nobody can act on is a rejection somebody retries verbatim."""
        with pytest.raises(RoutingConfigError, match="lowercase letters, digits and dashes"):
            profile_path("Open AI", root=tmp_path)

    def test_a_stray_file_that_is_not_a_valid_name_is_not_offered(self, tmp_path: Path) -> None:
        """Discovery and validation agree, so nothing is listed that cannot be chosen."""
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        (tmp_path / "model-routing.Backup Copy.yaml").write_text("version: '1'\n", encoding="utf-8")

        assert available_profiles(root=tmp_path) == ("openai",)


class TestLoadingAProfile:
    def test_a_profile_routes_where_its_file_says(self, tmp_path: Path) -> None:
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")

        policy = routing_policy("openai", root=tmp_path)

        assert policy.default.primary.provider == "openai"
        assert policy.version == "test-openai"

    def test_a_missing_profile_raises_rather_than_falling_back(self, tmp_path: Path) -> None:
        """The fall-back is the dangerous one.

        A project moved to ``openai`` because the local model could not fit its
        source, silently served the local policy, runs the model it was moved off
        and reports success for having done so. Failing here costs a message;
        falling through costs the reason anybody made the change.
        """
        (tmp_path / ROUTING_CONFIG_FILENAME).write_text("version: '1'\n", encoding="utf-8")

        with pytest.raises(RoutingConfigError, match="no routing profile 'openai'"):
            routing_policy("openai", root=tmp_path)

    def test_the_refusal_says_what_is_available(self, tmp_path: Path) -> None:
        write_profile(tmp_path, "groq", provider="groq", model="llama-4")

        with pytest.raises(RoutingConfigError, match="available: groq"):
            routing_policy("openai", root=tmp_path)


class TestTheShippedProfile:
    """The repository ships one, and it has to be more than a provider rename."""

    def test_it_loads(self) -> None:
        routing_policy(SHIPPED_PROFILE)

    def test_it_is_listed(self) -> None:
        assert SHIPPED_PROFILE in available_profiles()

    def test_every_stage_routes_to_openai(self) -> None:
        policy = routing_policy(SHIPPED_PROFILE)
        routes = [policy.default, *policy.stages.values()]

        providers = {choice.provider for route in routes for choice in _choices(route)}
        assert providers == {"openai"}

    def test_it_covers_the_same_stages_as_the_default(self) -> None:
        """A profile missing a stage does not fail — it falls to its own default.

        Which is the quiet kind of wrong: the stage runs, on the conservative
        fallback model, and nothing says the per-stage reasoning was skipped. So
        the profiles are held to the same stage list.
        """
        assert set(routing_policy(SHIPPED_PROFILE).stages) == set(default_routing_policy().stages)

    def test_it_sets_no_sampling_parameters(self) -> None:
        """The reasoning models reject ``temperature`` and ``top_p`` outright.

        The adapter sends only what the file sets, so one of these left in the
        file is a 400 on every call of that stage — which is exactly the failure
        ``writer llm probe`` exists to catch, and exactly the one that is free to
        avoid here.
        """
        policy = routing_policy(SHIPPED_PROFILE)
        routes = [policy.default, *policy.stages.values()]

        for route in routes:
            for choice in _choices(route):
                assert choice.temperature is None, f"{choice.model} sets temperature"
                assert choice.top_p is None, f"{choice.model} sets top_p"

    def test_it_allocates_no_context_window(self) -> None:
        """The field exists because Ollama allocates one per call. OpenAI does not."""
        policy = routing_policy(SHIPPED_PROFILE)
        routes = [policy.default, *policy.stages.values()]

        for route in routes:
            for choice in _choices(route):
                assert choice.context_window is None, f"{choice.model} sets context_window"

    def test_it_carries_its_own_version(self) -> None:
        """Every execution records the policy version, so two policies need two.

        A profile that shared the default's version string would make a run under
        it indistinguishable, in the record, from a run under the file it
        replaced — and the record is the only place that difference survives.
        """
        assert routing_policy(SHIPPED_PROFILE).version != default_routing_policy().version


def _choices(route: StageRoute) -> tuple[ModelChoice, ...]:
    """Both models a stage may use, or the one it has.

    Typed against the real classes rather than ``object``: every caller reaches
    for ``.provider`` or ``.temperature`` on what comes back, and an ``object``
    return turned one honest ignore here into seven attribute errors out there.
    """
    return (route.primary,) if route.fallback is None else (route.primary, route.fallback)


# ----------------------------------------------------------------------
# Reaching the call
# ----------------------------------------------------------------------


class TestRebindingTheGenerator:
    def test_the_clients_are_shared_not_rebuilt(self, harness: Harness, tmp_path: Path) -> None:
        """Choosing a profile must not cost a reconnect.

        The clients are connection pools and the prompt store is a cache. One
        generator per process, rebound per run, is the shape that keeps a profile
        a routing decision rather than a transport one.
        """
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        original = harness.runtime.generator

        rebound = original.with_routing(routing_policy("openai", root=tmp_path))

        assert rebound is not original
        assert rebound._clients is not None
        assert rebound._clients == original._clients
        assert rebound._prompts is original._prompts

    def test_the_original_is_left_alone(self, harness: Harness, tmp_path: Path) -> None:
        """Two runs of different projects share the process the moment there are two workers."""
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        original = harness.runtime.generator
        before = original.routing.version

        original.with_routing(routing_policy("openai", root=tmp_path))

        assert original.routing.version == before


class TestTheProjectsPolicyReachesTheCall:
    def test_a_project_that_has_chosen_nothing_gets_the_default(self, harness: Harness) -> None:
        project_id = new_project(harness)

        generator = generator_for(harness.runtime, project_id)

        assert generator is harness.runtime.generator

    def test_a_project_that_has_chosen_gets_its_own(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)
        harness.service.set_routing_profile(project_id, profile="openai", chosen_by=AUTHOR)

        generator = generator_for(harness.runtime, project_id)

        assert generator.routing.default.primary.provider == "openai"
        assert harness.runtime.generator.routing.default.primary.provider != "openai"


# ----------------------------------------------------------------------
# Choosing one
# ----------------------------------------------------------------------


class TestChoosingAProfile:
    def test_the_choice_is_reported_back(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)

        harness.service.set_routing_profile(project_id, profile="openai", chosen_by=AUTHOR)

        profiles = harness.service.routing_profiles(project_id)
        assert profiles.selected == "openai"
        assert profiles.policy_version == "test-openai"

    def test_the_default_is_reported_as_no_choice(self, harness: Harness) -> None:
        """``None``, not the string "default": the default file's identity is having no name."""
        profiles = harness.service.routing_profiles(new_project(harness))

        assert profiles.selected is None
        assert profiles.policy_version == default_routing_policy().version

    def test_a_project_can_be_moved_back(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        (tmp_path / ROUTING_CONFIG_FILENAME).write_text(SHIPPED_DEFAULT_TEXT, encoding="utf-8")
        project_id = new_project(harness)
        harness.service.set_routing_profile(project_id, profile="openai", chosen_by=AUTHOR)

        harness.service.set_routing_profile(project_id, profile=None, chosen_by=AUTHOR)

        assert harness.service.routing_profiles(project_id).selected is None

    def test_a_profile_with_no_file_is_refused_at_the_choice(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """So the failure lands on the person who can fix it.

        Left to the next stage, it arrives on a worker three commands later,
        attached to an editorial stage, and reads as a pipeline fault.
        """
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        project_id = new_project(harness)

        with pytest.raises(RoutingConfigError):
            harness.service.set_routing_profile(project_id, profile="groq", chosen_by=AUTHOR)

    def test_a_refused_choice_changes_nothing(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)
        harness.service.set_routing_profile(project_id, profile="openai", chosen_by=AUTHOR)

        with pytest.raises(RoutingConfigError):
            harness.service.set_routing_profile(project_id, profile="groq", chosen_by=AUTHOR)

        assert harness.service.routing_profiles(project_id).selected == "openai"

    def test_an_anonymous_choice_is_refused(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """It decides where this project's material is sent and what its calls cost."""
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)

        with pytest.raises(AttributionRequired):
            harness.service.set_routing_profile(project_id, profile="openai", chosen_by="")

    def test_the_choice_is_recorded_against_whoever_made_it(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)

        harness.service.set_routing_profile(project_id, profile="openai", chosen_by=AUTHOR)

        interventions: list[Any] = harness.runtime.session.query(_interventions()).all()
        payloads = [
            row.payload for row in interventions if "routing_profile" in (row.payload or {})
        ]
        assert payloads, "choosing a profile recorded no intervention"
        assert payloads[-1]["routing_profile"] == "openai"
        assert payloads[-1]["previous_routing_profile"] == ""

    def test_the_run_does_not_move(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Not a transition. A profile is about where the next call goes, not where the run is."""
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)
        before = harness.service.set_routing_profile(
            project_id, profile=None, chosen_by=AUTHOR
        ).state

        after = harness.service.set_routing_profile(
            project_id, profile="openai", chosen_by=AUTHOR
        ).state

        assert after == before


def _interventions() -> type:
    from groundscribe.provenance import models

    return models.UserIntervention


# ----------------------------------------------------------------------
# The two gates that were already there
# ----------------------------------------------------------------------


class TestSelectingIsNotConsenting:
    def test_choosing_a_profile_does_not_permit_its_provider(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Three statements, three people, and this command makes one of them.

        A project that selects ``openai`` without also naming it in
        ``allowed_providers`` has said where its calls should go and has not said
        its material may go there. The visibility surface is where that shows up,
        and it has to show up as *not permitted* rather than as a route that does
        not exist.
        """
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        (tmp_path / ROUTING_CONFIG_FILENAME).write_text(SHIPPED_DEFAULT_TEXT, encoding="utf-8")
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)

        harness.service.set_routing_profile(project_id, profile="openai", chosen_by=AUTHOR)

        visibility = harness.service.provider_visibility(project_id)
        assert visibility.stages, "no stages reported"
        assert all(not stage.permitted for stage in visibility.stages)

    def test_the_visibility_surface_follows_the_projects_own_profile(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The screen answers "where does *my* material go".

        Answered from the process's policy it would describe some other project's
        providers to whoever had just moved theirs — and this is the one screen a
        person consults precisely because they are unsure.
        """
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        (tmp_path / ROUTING_CONFIG_FILENAME).write_text(SHIPPED_DEFAULT_TEXT, encoding="utf-8")
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)

        before = harness.service.provider_visibility(project_id).routing_version
        harness.service.set_routing_profile(project_id, profile="openai", chosen_by=AUTHOR)
        after = harness.service.provider_visibility(project_id)

        assert before != after.routing_version
        assert after.routing_version == "test-openai"
        assert after.leaves_this_machine


# ----------------------------------------------------------------------
# Over HTTP
# ----------------------------------------------------------------------


class TestOverHttp:
    """The read rides the dashboard; only the command has a path of its own.

    Deliberately, and the guard in ``frontend/src/guards.test.ts`` is why: a
    screen may not name a command URL, and a read sharing a path with a ``PUT``
    is a command URL by that test's reckoning. Folding it into the dashboard also
    matches how every other screen here is fed — one composed read, not four
    small ones with four chances to be half-loaded.
    """

    def routing(self, client: TestClient, project_id: str) -> dict[str, Any]:
        response = client.get(f"/projects/{project_id}/dashboard")
        assert response.status_code == 200
        payload: dict[str, Any] = response.json()["routing"]
        return payload

    def test_a_project_reports_what_it_runs_against(
        self, client: TestClient, harness: Harness
    ) -> None:
        project_id = new_project(harness)

        routing = self.routing(client, project_id)

        assert routing["selected"] is None
        assert SHIPPED_PROFILE in routing["available"]
        assert routing["policy_version"] == default_routing_policy().version

    def test_the_dashboard_publishes_how_to_change_it(
        self, client: TestClient, harness: Harness
    ) -> None:
        """A client that built this path would hold a second copy of the routing table."""
        project_id = new_project(harness)

        command = self.routing(client, project_id)["command"]

        assert command["method"] == "PUT"
        assert command["path"] == f"/projects/{project_id}/routing-profile"
        assert command["requires_actor"] is True
        assert command["taken_by"] == "you"

    def test_a_profile_can_be_chosen_and_read_back(
        self, client: TestClient, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        (tmp_path / ROUTING_CONFIG_FILENAME).write_text(SHIPPED_DEFAULT_TEXT, encoding="utf-8")
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)

        put = client.put(
            f"/projects/{project_id}/routing-profile",
            json={"profile": "openai", "actor_id": AUTHOR},
        )

        assert put.status_code == 200
        routing = self.routing(client, project_id)
        assert routing["selected"] == "openai"
        assert routing["policy_version"] == "test-openai"

    def test_choosing_twice_leaves_it_where_choosing_once_did(
        self, client: TestClient, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``PUT``, and it means it: the body states what the profile is."""
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        (tmp_path / ROUTING_CONFIG_FILENAME).write_text(SHIPPED_DEFAULT_TEXT, encoding="utf-8")
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)
        body = {"profile": "openai", "actor_id": AUTHOR}

        client.put(f"/projects/{project_id}/routing-profile", json=body)
        client.put(f"/projects/{project_id}/routing-profile", json=body)

        assert self.routing(client, project_id)["selected"] == "openai"

    def test_null_moves_a_project_back_to_the_default(
        self, client: TestClient, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Required rather than optional, so clearing the box cannot read as "no change"."""
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        (tmp_path / ROUTING_CONFIG_FILENAME).write_text(SHIPPED_DEFAULT_TEXT, encoding="utf-8")
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)
        client.put(
            f"/projects/{project_id}/routing-profile",
            json={"profile": "openai", "actor_id": AUTHOR},
        )

        client.put(
            f"/projects/{project_id}/routing-profile",
            json={"profile": None, "actor_id": AUTHOR},
        )

        assert self.routing(client, project_id)["selected"] is None

    def test_a_profile_that_is_not_a_name_is_a_422(
        self, client: TestClient, harness: Harness
    ) -> None:
        """422, not 404: the project exists, and what is unusable is the body."""
        project_id = new_project(harness)

        response = client.put(
            f"/projects/{project_id}/routing-profile",
            json={"profile": "../../etc/passwd", "actor_id": AUTHOR},
        )

        assert response.status_code == 422

    def test_a_profile_with_no_file_is_a_422(
        self, client: TestClient, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        project_id = new_project(harness)

        response = client.put(
            f"/projects/{project_id}/routing-profile",
            json={"profile": "groq", "actor_id": AUTHOR},
        )

        assert response.status_code == 422

    def test_an_unknown_project_is_a_404(self, client: TestClient) -> None:
        response = client.put(
            "/projects/nope/routing-profile",
            json={"profile": None, "actor_id": AUTHOR},
        )

        assert response.status_code == 404

    def test_a_selected_profile_whose_file_has_gone_does_not_take_the_screen_down(
        self, client: TestClient, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The dashboard is where a person would go to fix exactly this.

        A read that raised would hide the control that undoes the problem, on the
        one screen that offers it — so the version is reported as unknown and the
        selection is reported as it stands.
        """
        monkeypatch.setenv(CONFIG_ROOT_ENV, str(tmp_path))
        (tmp_path / ROUTING_CONFIG_FILENAME).write_text(SHIPPED_DEFAULT_TEXT, encoding="utf-8")
        write_profile(tmp_path, "openai", provider="openai", model="gpt-5")
        project_id = new_project(harness)
        harness.service.set_routing_profile(project_id, profile="openai", chosen_by=AUTHOR)
        (tmp_path / "model-routing.openai.yaml").unlink()

        routing = self.routing(client, project_id)

        assert routing["selected"] == "openai"
        assert routing["policy_version"] == ""


# ----------------------------------------------------------------------
# The column
# ----------------------------------------------------------------------


def test_a_project_starts_on_the_default(db_session: Session) -> None:
    """NULL is the answer for every project that has not chosen, not a placeholder."""
    db_session.add(domain_models.User(id="u-rp", name="Ada", email="ada@example.com"))
    db_session.flush()
    db_session.add(domain_models.Project(id="p-rp", user_id="u-rp", title="Untouched"))
    db_session.flush()

    untouched = db_session.get(domain_models.Project, "p-rp")
    assert untouched is not None
    assert untouched.routing_profile is None
