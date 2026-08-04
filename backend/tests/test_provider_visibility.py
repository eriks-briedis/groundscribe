"""What a person can see about where their material goes (phase 13).

Spec (plan/13 → *Provider visibility surface*: which provider/model receives the
source, local vs external, whether confidential sections exist, what content is
sent, what is preserved in the trace — rendered in the phase-11 UI, data provided
here. Test-first: *Provider-visibility data*).

plan/00 promises *local-first by default, with visible data flow to external
providers*. "Visible" is the word this module has to earn, and the standard is
higher than listing configuration: the surface has to answer the questions a
person actually has before they press a button.

- **Who gets it, and is that off this machine?** A provider name alone does not
  say. ``ollama`` on localhost and a hosted API are the same shape of string and
  completely different decisions.
- **Is there anything sensitive in this project at all?** Counted, never quoted.
  A screen that displayed the confidential passages in order to warn about them
  would be the leak it was drawn to prevent.
- **What is actually sent, and what is withheld?** The counts come from the same
  flags the request builder enforces, so the screen cannot promise something the
  pipeline does not do.
- **What survives afterwards?** The retention mode, stated in the same place —
  because "who sees it now" and "what is kept forever" are the two halves of one
  question and a person asking either is asking both.

A stage the project has not permitted is reported as *not permitted* rather than
omitted. Omission would make a forbidden route look like a route that does not
exist, and the difference is the whole point of an allow-list.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.confidentiality import Confidentiality, Exclusion
from groundscribe.domain.enums import ArticleDepth, SegmentKind
from groundscribe.domain.retention import RetentionMode
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.llm.routing import RoutingPolicy, default_routing_policy
from groundscribe.privacy.visibility import LOCAL_PROVIDERS, provider_visibility

LOCAL = "ollama"

CONSTRAINTS = EditorialConstraints(
    audience="platform engineers",
    platform="blog",
    depth=ArticleDepth.PRACTITIONER,
    allowed_providers=(LOCAL,),
)


def _seed(session: Session) -> str:
    """A project with one publishable, one internal and one confidential passage."""
    user = domain_models.User(id="user-vis", name="Ada", email="ada@example.com")
    project = domain_models.Project(id="proj-vis", user_id=user.id, title="Postmortem")
    document = domain_models.SourceDocument(id="doc-vis", project_id=project.id, title="Postmortem")
    session.add_all([user, project])
    session.flush()
    session.add(document)
    session.flush()
    session.add_all(
        [
            domain_models.SourceSegment(
                id="seg-public",
                document_id=document.id,
                ordinal=0,
                text="We shipped a read-through cache in March.",
                kind=SegmentKind.PARAGRAPH,
            ),
            domain_models.SourceSegment(
                id="seg-internal",
                document_id=document.id,
                ordinal=1,
                text="The invalidation bug was reported three weeks before we saw it.",
                kind=SegmentKind.PARAGRAPH,
                confidentiality=Confidentiality.INTERNAL,
            ),
            domain_models.SourceSegment(
                id="seg-secret",
                document_id=document.id,
                ordinal=2,
                text="Northwind threatened to terminate the contract.",
                kind=SegmentKind.PARAGRAPH,
                confidentiality=Confidentiality.CONFIDENTIAL,
            ),
        ]
    )
    session.flush()
    return project.id


def test_every_stage_names_its_provider_and_model(db_session: Session) -> None:
    """The first question: who receives this."""
    project_id = _seed(db_session)

    surface = provider_visibility(
        db_session, project_id, constraints=CONSTRAINTS, routing=default_routing_policy()
    )

    assert surface.stages
    for stage in surface.stages:
        assert stage.provider
        assert stage.model
        assert stage.stage


def test_a_local_provider_is_reported_as_local(db_session: Session) -> None:
    """A provider name does not say whether the material leaves the machine.

    ``ollama`` and a hosted API are the same shape of string and completely
    different decisions, so the surface answers the question rather than leaving
    it to whoever reads the name.

    Asserted against a routing policy pointed at a local provider rather than
    against the shipped one. A test that read "local" off whatever happened to be
    configured would be testing the configuration instead of the surface — and
    would flip its own meaning every time routing changed, which by now it has
    done twice in both directions.
    """
    project_id = _seed(db_session)

    surface = provider_visibility(
        db_session,
        project_id,
        constraints=CONSTRAINTS,
        routing=_routed_to("ollama"),
    )

    assert all(stage.local for stage in surface.stages)
    assert not surface.leaves_this_machine


def test_a_hosted_provider_is_reported_as_leaving_the_machine(
    db_session: Session,
) -> None:
    """The other half of the pair, and the one with consequences.

    Held against an explicitly hosted policy rather than the shipped one for the
    same reason its local twin is: the assertion has to keep meaning what it says
    whichever way the shipped config happens to point this month.
    """
    project_id = _seed(db_session)

    surface = provider_visibility(
        db_session, project_id, constraints=CONSTRAINTS, routing=_routed_to("openai")
    )

    assert surface.leaves_this_machine
    assert all(not stage.local for stage in surface.stages)


def test_the_shipped_routing_is_reported_as_whatever_it_actually_is(
    db_session: Session,
) -> None:
    """The config may point anywhere; the surface has to agree with it.

    This is the property that survives a routing change, and the previous version
    of this test is why it is written this way. It pinned the shipped policy's
    provider as a literal, and said in its own docstring that the shipped policy
    "is OpenAI now" — so when routing moved back to local models it failed, having
    tested the configuration rather than the surface that reports it.

    What a person actually asks is *does my material leave this machine?* The
    answer must be derived from the routing file every time, never from a promise
    made when the default was something else.
    """
    project_id = _seed(db_session)
    routing = default_routing_policy()

    surface = provider_visibility(db_session, project_id, constraints=CONSTRAINTS, routing=routing)

    routed = {
        choice.provider
        for route in [routing.default, *routing.stages.values()]
        for choice in (route.primary, route.fallback)
        if choice is not None
    }
    assert {stage.provider for stage in surface.stages} <= routed
    for stage in surface.stages:
        assert stage.local is (stage.provider in LOCAL_PROVIDERS)
    assert surface.leaves_this_machine is any(not stage.local for stage in surface.stages)


def _routed_to(provider: str) -> RoutingPolicy:
    """The shipped policy with every route pointed at one provider.

    Built from the real policy so the stage list stays the real stage list; only
    the provider moves, which is the one variable these tests are about.
    """
    policy = default_routing_policy()
    stages = {
        name: route.model_copy(
            update={
                "primary": route.primary.model_copy(update={"provider": provider}),
                "fallback": (
                    route.fallback.model_copy(update={"provider": provider})
                    if route.fallback is not None
                    else None
                ),
            }
        )
        for name, route in policy.stages.items()
    }
    return policy.model_copy(update={"stages": stages})


def test_a_hosted_provider_is_reported_as_external(db_session: Session) -> None:
    """The case the whole surface exists for."""
    project_id = _seed(db_session)
    constraints = CONSTRAINTS.model_copy(update={"allowed_providers": ("anthropic",)})
    routing = default_routing_policy().model_copy(deep=True)

    surface = provider_visibility(
        db_session,
        project_id,
        constraints=constraints,
        routing=routing,
        local_providers=frozenset(),
    )

    assert surface.leaves_this_machine
    assert all(not stage.local for stage in surface.stages)


def test_a_provider_the_project_has_not_allowed_is_shown_as_refused(
    db_session: Session,
) -> None:
    """Not omitted. A forbidden route and a missing route are different facts.

    Dropping it would make the allow-list look like a list of what exists rather
    than a list of what is permitted, which is the opposite of what it is.
    """
    project_id = _seed(db_session)
    constraints = CONSTRAINTS.model_copy(update={"allowed_providers": ()})

    surface = provider_visibility(
        db_session, project_id, constraints=constraints, routing=default_routing_policy()
    )

    assert surface.stages
    assert all(not stage.permitted for stage in surface.stages)


def test_confidential_material_is_counted_and_never_quoted(db_session: Session) -> None:
    """A screen that showed the passages in order to warn about them is the leak."""
    project_id = _seed(db_session)

    surface = provider_visibility(
        db_session, project_id, constraints=CONSTRAINTS, routing=default_routing_policy()
    )

    assert surface.confidential_segments == 1
    assert surface.internal_segments == 1
    assert "Northwind" not in repr(surface)


def test_what_is_sent_and_what_is_withheld_are_both_reported(db_session: Session) -> None:
    """Counted from the same flags the request builder enforces.

    Reading a different source would let the screen promise something the
    pipeline does not do, and the screen is what a person trusts.
    """
    project_id = _seed(db_session)

    surface = provider_visibility(
        db_session, project_id, constraints=CONSTRAINTS, routing=default_routing_policy()
    )

    assert surface.segments_sent == 2
    assert surface.segments_withheld == 1


def test_an_explicit_input_exclusion_counts_as_withheld(db_session: Session) -> None:
    """Publishable material can still be kept off the wire."""
    project_id = _seed(db_session)
    segment = db_session.get(domain_models.SourceSegment, "seg-public")
    assert segment is not None
    segment.excluded = [Exclusion.MODEL_INPUT.value]
    db_session.flush()

    surface = provider_visibility(
        db_session, project_id, constraints=CONSTRAINTS, routing=default_routing_policy()
    )

    assert surface.segments_sent == 1
    assert surface.segments_withheld == 2


def test_what_the_trace_keeps_is_reported_beside_who_sees_it(db_session: Session) -> None:
    """Two halves of one question, so they are answered in one place.

    Someone asking "who sees this?" is also asking "and what is kept?", and a
    surface that answered only the first would send them looking for a settings
    screen to answer the second.
    """
    project_id = _seed(db_session)
    constraints = CONSTRAINTS.model_copy(
        update={"trace_retention_mode": RetentionMode.NO_RAW_PROVIDER_PAYLOADS}
    )

    surface = provider_visibility(
        db_session, project_id, constraints=constraints, routing=default_routing_policy()
    )

    assert surface.retention_mode is RetentionMode.NO_RAW_PROVIDER_PAYLOADS
    assert "raw_response" not in surface.trace_preserves
    assert "effective_request" in surface.trace_preserves
