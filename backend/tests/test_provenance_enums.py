"""Provenance vocabulary tests (phase 03).

Spec (plan/03 → Test-first specification): retry *types* must be distinguishable
(network, rate-limit, provider-error, invalid-schema, content-repair,
model-fallback, manual, prompt-modified) rather than collapsed into a bare count,
and tool invocations must record their initiator (model-selected vs
pipeline-mandated).

These enums are the fixed vocabularies every later provenance record routes on,
so they are pinned exhaustively here: an accidental rename or deletion is a
breaking change to stored provenance and must fail loudly.
"""

from __future__ import annotations

from enum import StrEnum

from groundscribe.provenance import enums


def _values(enum_cls: type[StrEnum]) -> set[str]:
    return {member.value for member in enum_cls}


def test_retry_types_cover_exactly_the_eight_named_kinds() -> None:
    """The spec names eight retry causes; a bare count is not acceptable."""
    assert _values(enums.RetryType) == {
        "network",
        "rate_limit",
        "provider_error",
        "invalid_schema",
        "content_repair",
        "model_fallback",
        "manual",
        "prompt_modified",
    }


def test_invocation_outcomes_distinguish_useful_but_invalid_responses() -> None:
    """A response can fail JSON parsing, fail schema validation, or be accepted.

    Keeping these distinct is what lets a useful-but-invalid response be preserved
    alongside its repaired successor.
    """
    assert _values(enums.InvocationOutcome) == {
        "accepted",
        "invalid_json",
        "invalid_schema",
        "refused",
        "timeout",
        "provider_error",
        "rate_limited",
        "cancelled",
    }


def test_execution_status_separates_failure_from_cancellation() -> None:
    """Failure and cancellation are different endings; both retain their trace."""
    assert _values(enums.ExecutionStatus) == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    }


def test_actor_types_name_who_or_what_acted() -> None:
    """Every trace event and decision names an actor; ``policy`` is not a person."""
    assert _values(enums.ActorType) == {"user", "model", "policy", "tool", "system"}


def test_tool_initiator_distinguishes_model_choice_from_pipeline_mandate() -> None:
    assert _values(enums.ToolInitiator) == {"model_selected", "pipeline_mandated"}


def test_artifact_direction_distinguishes_consumed_from_produced() -> None:
    """One execution-artefact table serves both directions, so the value carries them."""
    assert _values(enums.ArtifactDirection) == {"input", "output"}


def test_context_dispositions_cover_selected_excluded_and_truncated() -> None:
    """Every context candidate ends up in exactly one of these three states."""
    assert _values(enums.ContextDisposition) == {"selected", "excluded", "truncated"}


def test_intervention_types_cover_the_human_control_points() -> None:
    assert _values(enums.InterventionType) == {
        "approval",
        "rejection",
        "edit",
        "override",
        "answer",
        "cancellation",
    }


def test_enum_members_are_stable_strings() -> None:
    """Values are stored verbatim in provenance records, so they must be strings."""
    for enum_cls in (
        enums.RetryType,
        enums.InvocationOutcome,
        enums.ExecutionStatus,
        enums.ActorType,
        enums.ArtifactDirection,
        enums.ToolInitiator,
        enums.ContextDisposition,
        enums.InterventionType,
    ):
        assert issubclass(enum_cls, StrEnum)
        for member in enum_cls:
            assert member == member.value
