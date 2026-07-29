"""What a fork is allowed to change (phase 12).

plan/12 names the variables: *prompt version, model, temperature, voice profile,
rubric, source model, context-selection strategy, revision plan*. This is that
list, as a closed vocabulary, plus the translation from a variable into the thing
the stage already accepts.

Closed for the reason the trace filters are closed, and the seven architecture
operations before them: a name the system does not recognise has to be refused.
An experiment whose candidate configuration was silently dropped does not fail —
it *succeeds*, and reports that the change made no difference, which is the one
outcome nobody would think to check.

The translation is deliberately thin. Every variable here already exists as a
parameter of the stage that honours it — ``template_version`` on the prompt,
``RouteOverride`` on the call, a voice profile on the drafting stages — because
phase 04 and phase 07 built them to be overridable one run at a time. Fork does
not add a mechanism; it names which of them an experiment is moving.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from groundscribe.llm.routing import RouteOverride
from groundscribe.stages.context import ContextStrategy


class ForkVariable(StrEnum):
    """The variables plan/12 says a fork may alter, and no others."""

    PROMPT_VERSION = "prompt_version"
    MODEL = "model"
    PROVIDER = "provider"
    TEMPERATURE = "temperature"
    VOICE_PROFILE = "voice_profile"
    RUBRIC_VERSION = "rubric_version"
    SOURCE_MODEL = "source_model"
    CONTEXT_STRATEGY = "context_strategy"
    REVISION_PLAN = "revision_plan"


class ForkVariables(BaseModel):
    """One fork's changes, validated against the vocabulary before anything runs.

    A model rather than a dict so the refusal happens at the edge, in the same
    breath as the request that carried it, rather than three layers down where
    the only honest thing left to do is fail a job.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    prompt_version: str | None = None
    model: str | None = None
    provider: str | None = None
    temperature: float | None = None
    voice_profile: str | None = None
    rubric_version: str | None = None
    source_model: str | None = None
    context_strategy: str | None = None
    revision_plan: str | None = None

    @model_validator(mode="after")
    def _named_strategies_exist(self) -> ForkVariables:
        """A context strategy nothing implements is refused here, not ignored later.

        The closed vocabulary one level down. ``context_strategy`` carries the
        *name* of a selection strategy, and a run that quietly fell back to the
        default on an unrecognised name would report that retrieval made no
        difference — the same silent-drop failure this class exists to prevent
        for the variables themselves.

        Empty is still allowed: a fork with nothing changed is a replay, and the
        caller is told so rather than refused.
        """
        if self.context_strategy is None:
            return self
        known = {strategy.value for strategy in ContextStrategy}
        if self.context_strategy not in known:
            raise ValueError(
                f"unknown context strategy {self.context_strategy!r}; "
                f"expected one of {', '.join(sorted(known))}"
            )
        return self

    @property
    def strategy(self) -> ContextStrategy | None:
        """The named strategy, already validated to exist."""
        return ContextStrategy(self.context_strategy) if self.context_strategy else None

    @property
    def empty(self) -> bool:
        return not self.changes

    @property
    def changes(self) -> dict[str, Any]:
        """Only what was actually named, so a record shows the change and not the schema."""
        return {
            name: value for name, value in self.model_dump(mode="json").items() if value is not None
        }

    def route_override(self, *, requested_by: str, reason: str = "") -> RouteOverride | None:
        """The routing change, if this fork makes one.

        ``None`` when it does not: an override recorded for a fork that altered
        no routing would put a decision in the trace saying a model was chosen
        deliberately when it was inherited.
        """
        routing = {
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
        }
        if not any(value is not None for value in routing.values()):
            return None
        return RouteOverride(requested_by=requested_by, reason=reason, **routing)


class ForkRequest(BaseModel):
    """A person asking for a fork, with what they want changed."""

    model_config = ConfigDict(extra="forbid")

    actor_id: str = Field(min_length=1)
    reason: str = ""
    variables: ForkVariables = ForkVariables()


__all__ = ["ForkRequest", "ForkVariable", "ForkVariables"]
