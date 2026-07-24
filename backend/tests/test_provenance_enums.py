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

from groundscribe.domain.enums import ArtifactType
from groundscribe.provenance import enums

#: The editorial artefact kinds pinned by phase 02.
EDITORIAL_ARTIFACT_TYPES = {
    "source_document",
    "source_model",
    "content_architecture",
    "article_concept",
    "article_brief",
    "article_version",
    "review",
    "revision_plan",
    "voice_profile",
    "validation_report",
}

#: The provenance payloads phase 03 stores as content-addressed snapshots.
PROVENANCE_ARTIFACT_TYPES = {
    "effective_request",
    "raw_response",
    "parsed_response",
    "validated_response",
}


def _values(enum_cls: type[StrEnum]) -> set[str]:
    return {member.value for member in enum_cls}


def test_provenance_payloads_are_snapshotted_artifact_types() -> None:
    """The effective request and each response form are content-addressed artefacts.

    plan/03 requires raw, parsed and validated responses to be stored as
    *separate snapshots*, which means the snapshot store's type vocabulary has to
    name them.
    """
    assert _values(ArtifactType) >= PROVENANCE_ARTIFACT_TYPES


def test_artifact_type_is_exactly_the_editorial_plus_provenance_kinds() -> None:
    """One enum, two groups, nothing stray: total membership is pinned here."""
    assert _values(ArtifactType) == EDITORIAL_ARTIFACT_TYPES | PROVENANCE_ARTIFACT_TYPES


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
