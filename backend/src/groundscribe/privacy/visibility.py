"""What a person can see about where their material goes (phase 13).

plan/13 → *Provider visibility surface*: which provider/model receives the
source, local vs external, whether confidential sections exist, what content is
sent, what is preserved in the trace. The phase-11 UI renders it; this is the
data behind it.

plan/00 promises *local-first by default, with visible data flow to external
providers*, and "visible" is what this module has to earn. Listing configuration
is not enough — the surface answers the questions a person has before they press
a button:

- **Who gets it, and does it leave this machine?** ``ollama`` on localhost and a
  hosted API are the same shape of string and completely different decisions, so
  the answer is computed rather than left to whoever reads the name.
- **Is there anything sensitive here?** Counted, never quoted. A screen that
  displayed the confidential passages in order to warn about them would be the
  leak it was drawn to prevent.
- **What is sent, and what is withheld?** From the same flags the request builder
  enforces, so the screen cannot promise something the pipeline does not do.
- **What survives afterwards?** The retention mode, in the same place — "who sees
  this now" and "what is kept" are two halves of one question.

A provider the project has not permitted is reported as refused, not omitted:
omission would make a forbidden route look like a route that does not exist,
which is the opposite of what an allow-list says.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.confidentiality import Confidentiality
from groundscribe.domain.retention import RetentionMode
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.llm.routing import RoutingPolicy
from groundscribe.privacy.retention import PERMITTED

#: Providers that run on the machine the pipeline runs on.
#:
#: A list rather than a property of the adapter, because it is a *deployment*
#: fact: the same provider name can be a process on localhost or a box in another
#: building, and only the person who set it up knows which. Overridable for
#: exactly that reason.
LOCAL_PROVIDERS = frozenset({"ollama", "llamacpp", "local"})


@dataclass(frozen=True)
class StageVisibility:
    """Where one stage's material goes."""

    stage: str
    provider: str
    model: str
    local: bool
    permitted: bool
    fallback_provider: str | None = None
    fallback_model: str | None = None


@dataclass(frozen=True)
class ProviderVisibility:
    """Everything the data-flow screen needs, and nothing it must not show.

    Counts, never content. This object is rendered, logged and sometimes pasted
    into a support thread; a field holding the confidential text would travel
    everywhere the warning does.
    """

    project_id: str
    stages: tuple[StageVisibility, ...]
    routing_version: str
    confidential_segments: int
    internal_segments: int
    segments_sent: int
    segments_withheld: int
    retention_mode: RetentionMode
    trace_preserves: tuple[str, ...]

    @property
    def leaves_this_machine(self) -> bool:
        """Whether any stage sends material to a provider that is not local."""
        return any(not stage.local for stage in self.stages)

    @property
    def has_confidential_material(self) -> bool:
        return bool(self.confidential_segments)


def provider_visibility(
    session: Session,
    project_id: str,
    *,
    constraints: EditorialConstraints,
    routing: RoutingPolicy,
    local_providers: frozenset[str] = LOCAL_PROVIDERS,
) -> ProviderVisibility:
    """The data-flow surface for one project."""
    stages = tuple(
        _stage(routing, stage, constraints=constraints, local_providers=local_providers)
        for stage in sorted(routing.stages)
    )
    segments = session.scalars(
        select(domain_models.SourceSegment)
        .join(
            domain_models.SourceDocument,
            domain_models.SourceSegment.document_id == domain_models.SourceDocument.id,
        )
        .where(domain_models.SourceDocument.project_id == project_id)
    ).all()

    flags = [segment.flags for segment in segments]
    return ProviderVisibility(
        project_id=project_id,
        stages=stages,
        routing_version=routing.version,
        confidential_segments=sum(
            1 for flag in flags if flag.classification is Confidentiality.CONFIDENTIAL
        ),
        internal_segments=sum(
            1 for flag in flags if flag.classification is Confidentiality.INTERNAL
        ),
        # The same predicate the request builder uses, deliberately: a screen
        # that counted by classification would disagree with the pipeline the
        # moment someone set an explicit exclusion.
        segments_sent=sum(1 for flag in flags if flag.may_be_sent_to_a_provider),
        segments_withheld=sum(1 for flag in flags if not flag.may_be_sent_to_a_provider),
        retention_mode=constraints.trace_retention_mode,
        trace_preserves=tuple(
            sorted(kind.value for kind in PERMITTED[constraints.trace_retention_mode])
        ),
    )


def _stage(
    routing: RoutingPolicy,
    stage: str,
    *,
    constraints: EditorialConstraints,
    local_providers: frozenset[str],
) -> StageVisibility:
    route = routing.resolve(stage)
    fallback = route.fallback
    return StageVisibility(
        stage=stage,
        provider=route.primary.provider,
        model=route.primary.model,
        local=route.primary.provider in local_providers,
        permitted=constraints.permits_provider(route.primary.provider),
        fallback_provider=fallback.provider if fallback is not None else None,
        fallback_model=fallback.model if fallback is not None else None,
    )


__all__ = [
    "LOCAL_PROVIDERS",
    "ProviderVisibility",
    "StageVisibility",
    "provider_visibility",
]
