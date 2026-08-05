"""Per-stage model routing (phase 04).

plan/04 → *Model routing*: which provider, model and parameters each stage uses
is configuration, not a constant at a call site. Extraction wants a strong
structured model, drafting a prose model, validation something cheap and
deterministic; scattering those choices through the stage code makes them
impossible to audit, to override for one run, or to change without a deploy.

Versioning here is a declared string in one file rather than a directory of
version files (as prompts use). The difference is deliberate: a run can pin an
*old* prompt version — evaluations and experiments need exactly that — whereas a
run always resolves routing once, at the start, and records the version it got.
Superseded routing configs therefore live in git history, and the execution
record names the version it ran under.

*Profiles* (phase 15) are the same file, more than once. A machine that has both
a local model and a hosted key has two coherent sets of routing choices, not one
with a provider field to flip: the models differ, and so does everything sized
around them — ``context_window`` exists because Ollama allocates one per call,
and the "degrade the window" fallback is a local memory decision that means
nothing on a metered API. So a profile is a whole policy file, named beside the
default, and a project picks one by name. Flipping a provider on a route built
for another provider produces a config that is valid, wrong, and quiet about it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from groundscribe.llm.enums import StructuredOutputMode
from groundscribe.llm.protocol import ProviderMetadata, RetryPolicy, RuntimeConfig
from groundscribe.paths import config_root

#: Filename of the shipped routing configuration under the config root.
ROUTING_CONFIG_FILENAME = "model-routing.yaml"

#: A named profile's filename, beside the default rather than under it. Siblings
#: because they are alternatives of equal standing — the default is the one a
#: project gets for not choosing, not the one the others derive from.
ROUTING_PROFILE_TEMPLATE = "model-routing.{profile}.yaml"

#: What a profile name may be.
#:
#: A profile name arrives from a person and becomes a path, so it is validated as
#: a *name* rather than sanitised as a path: lowercase, digits and dashes, and
#: nothing else. ``../`` and an absolute path both fail the pattern instead of
#: being stripped, which is the difference between refusing a bad name and
#: quietly accepting a corrected one.
PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class RoutingConfigError(Exception):
    """The routing configuration is missing, malformed, or asked for something
    it does not declare."""


class ModelChoice(BaseModel):
    """One provider/model/parameter set a stage may run against."""

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    provider: str
    model: str
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    max_output_tokens: int | None = None
    #: Context window to allocate for the call, for providers that allocate one
    #: per request. Locally hosted models default it low and truncate the prompt
    #: silently above it, so a stage whose input is large has to say so here.
    context_window: int | None = None
    reasoning_effort: str | None = None
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.NATIVE_SCHEMA
    tool_choice: str | None = None
    stop_sequences: tuple[str, ...] = ()
    timeout_seconds: float | None = None


class StageRoute(BaseModel):
    """How one stage is routed: its model, and where the ladder escalates to.

    The fallback is declared per stage rather than globally because "cheaper but
    steadier" means a different model for extraction than for drafting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: ModelChoice
    fallback: ModelChoice | None = None
    rationale: str = ""


class RouteOverride(BaseModel):
    """A one-run change to a stage's routing, and who asked for it.

    ``requested_by`` is mandatory. The override is recorded as a decision, and
    phase 03 refuses to store a decision nobody is accountable for — so an
    anonymous override is rejected here rather than failing later at the write.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    requested_by: str
    reason: str = ""
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    max_output_tokens: int | None = None
    context_window: int | None = None
    reasoning_effort: str | None = None
    structured_output_mode: StructuredOutputMode | None = None
    tool_choice: str | None = None
    stop_sequences: tuple[str, ...] | None = None
    timeout_seconds: float | None = None

    @model_validator(mode="after")
    def _overrides_are_attributable(self) -> Self:
        if not self.requested_by:
            raise ValueError("requested_by is required: an anonymous override is unreviewable")
        return self

    def changes(self) -> dict[str, Any]:
        """The routing fields this override actually changes.

        Excludes ``requested_by``/``reason``: they say who and why, not what, and
        mixing them into the diff would make the recorded change unreadable.
        """
        return self.model_dump(exclude={"requested_by", "reason"}, exclude_none=True, mode="json")


class ResolvedRoute(BaseModel):
    """The routing outcome for one stage execution, with its provenance.

    Carries what was decided *and* how it was arrived at — policy version,
    whether the default was used, what an override changed and who asked — so the
    decision record written from it is complete without a second lookup.
    """

    model_config = ConfigDict(frozen=True)

    stage: str
    policy_version: str
    primary: ModelChoice
    fallback: ModelChoice | None = None
    used_default: bool = False
    overrides: dict[str, Any] = Field(default_factory=dict)
    overridden_by: str | None = None

    def choice(self, *, use_fallback: bool = False) -> ModelChoice:
        """The model this call should use, primary or fallback."""
        if not use_fallback:
            return self.primary
        if self.fallback is None:
            raise RoutingConfigError(f"stage {self.stage!r} declares no fallback model")
        return self.fallback

    def runtime_config(
        self,
        metadata: ProviderMetadata,
        retry_policy: RetryPolicy,
        *,
        use_fallback: bool = False,
    ) -> RuntimeConfig:
        """Build the runtime configuration recorded with the invocation.

        Provider and model come from the *route* — that is what was asked for,
        and it is what makes a fallback swap visible in the record — while the
        revision and library versions come from the client that answered.
        """
        choice = self.choice(use_fallback=use_fallback)
        return RuntimeConfig(
            provider=choice.provider,
            model=choice.model,
            model_revision=metadata.model_revision,
            temperature=choice.temperature,
            top_p=choice.top_p,
            seed=choice.seed,
            max_output_tokens=choice.max_output_tokens,
            context_window=choice.context_window,
            reasoning_effort=choice.reasoning_effort,
            structured_output_mode=choice.structured_output_mode,
            tool_choice=choice.tool_choice,
            stop_sequences=choice.stop_sequences,
            api_version=metadata.api_version,
            client_version=metadata.client_version,
            timeout_seconds=choice.timeout_seconds,
            retry_policy=retry_policy,
        )


class RoutingPolicy(BaseModel):
    """A versioned map from stage name to model choice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    description: str = ""
    default: StageRoute
    stages: dict[str, StageRoute] = Field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> RoutingPolicy:
        """Load a routing policy from a YAML file."""
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise RoutingConfigError(f"cannot read routing config {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise RoutingConfigError(f"invalid YAML in routing config {path}: {exc}") from exc
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise RoutingConfigError(f"invalid routing config {path}: {exc}") from exc

    def resolve(self, stage: str, *, override: RouteOverride | None = None) -> ResolvedRoute:
        """Resolve one stage's route, applying and capturing any override."""
        route = self.stages.get(stage)
        changes = override.changes() if override is not None else {}
        primary = route.primary if route is not None else self.default.primary
        if changes:
            primary = primary.model_copy(update=changes)
        return ResolvedRoute(
            stage=stage,
            policy_version=self.version,
            primary=primary,
            fallback=route.fallback if route is not None else self.default.fallback,
            used_default=route is None,
            overrides=changes,
            overridden_by=override.requested_by if override is not None else None,
        )


def profile_path(profile: str | None, *, root: Path | None = None) -> Path:
    """Where ``profile``'s policy file lives. ``None`` is the default file.

    Raises :class:`RoutingConfigError` for a name that is not a name. The check
    is here, at the one place a profile becomes a path, so every caller that
    reaches the filesystem is behind it.
    """
    base = root if root is not None else config_root()
    if profile is None:
        return base / ROUTING_CONFIG_FILENAME
    if not PROFILE_NAME.match(profile):
        raise RoutingConfigError(
            f"invalid routing profile name {profile!r}: lowercase letters, digits and dashes only"
        )
    return base / ROUTING_PROFILE_TEMPLATE.format(profile=profile)


def available_profiles(*, root: Path | None = None) -> tuple[str, ...]:
    """Every profile this installation ships a file for, sorted.

    Discovered by listing rather than declared in a registry, for the reason the
    policies themselves are files: adding a profile should be adding a file. The
    default is not in the list — it is what "no profile" means, and offering it
    as a choice alongside the others would make "openai" and "the default, which
    is currently ollama" look like the same kind of answer.
    """
    base = root if root is not None else config_root()
    if not base.is_dir():
        return ()
    prefix, suffix = ROUTING_PROFILE_TEMPLATE.split("{profile}")
    found = {
        name
        for path in base.glob(ROUTING_PROFILE_TEMPLATE.format(profile="*"))
        if PROFILE_NAME.match(name := path.name[len(prefix) : -len(suffix)])
    }
    return tuple(sorted(found))


def routing_policy(profile: str | None = None, *, root: Path | None = None) -> RoutingPolicy:
    """The routing policy for ``profile``, or the shipped default for ``None``.

    A named profile whose file is absent is an error rather than a fall-through
    to the default. Falling through would run the project on the provider it had
    just been moved off, and report success for having done so.
    """
    path = profile_path(profile, root=root)
    if profile is not None and not path.is_file():
        known = ", ".join(available_profiles(root=root)) or "none"
        raise RoutingConfigError(f"no routing profile {profile!r} at {path} (available: {known})")
    return RoutingPolicy.from_yaml(path)


def default_routing_policy() -> RoutingPolicy:
    """The shipped routing policy from the config root."""
    return routing_policy(None)
