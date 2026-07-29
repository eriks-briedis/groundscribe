"""What groundscribe promises about repeating itself (phase 12).

plan/12 → *Reproducibility contract: promise complete inspection + configuration
preservation + repeatable deterministic operations + linked replays +
original-vs-replay comparison; do not promise bit-for-bit reproduction of hosted
models.*

Data rather than prose, for two reasons.

**It is shown to people.** A person deciding whether to trust a replay is asking
exactly this question, and answering it in a README is answering it somewhere
they are not. The API serves this list; the run-comparison screen shows it beside
the two executions it is comparing.

**Prose widens.** A sentence describing a system as "reproducible" is true of the
provenance and false of the model, and the gap between those two readings is
where a person decides that a replay proved something. Each promise here is a
single claim carrying the name of the test that demonstrates it, and the one
refusal is written down beside them — a contract listing only its guarantees
invites the reader to assume the rest.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Guarantee:
    """One thing the system does or does not promise about repeating work.

    ``evidence`` names the test that demonstrates it. A promise nothing
    demonstrates is a marketing claim, and naming the test is what lets a guard
    notice when the two part company.
    """

    name: str
    title: str
    detail: str
    promised: bool
    evidence: str


#: What repeating work in groundscribe does guarantee.
REPRODUCIBILITY: tuple[Guarantee, ...] = (
    Guarantee(
        name="complete_inspection",
        title="Complete inspection",
        detail=(
            "Every stage execution can be opened afterwards and shows what actually "
            "happened: the context it selected, the exact request it sent, every model "
            "call including the ones that failed, the decisions taken, the artefacts "
            "produced and the events in order."
        ),
        promised=True,
        evidence="test_inspecting_shows_exactly_what_was_recorded",
    ),
    Guarantee(
        name="configuration_preservation",
        title="Preserved configuration",
        detail=(
            "A replay runs under the configuration the original ran under — the same "
            "prompt template and version, provider, model and parameters — and reads the "
            "inputs the original read rather than whatever is newest."
        ),
        promised=True,
        evidence="test_a_replay_reads_the_inputs_the_original_read",
    ),
    Guarantee(
        name="deterministic_operations",
        title="Deterministic operations repeat exactly",
        detail=(
            "The parts of the pipeline that involve no model give the same answer every "
            "time: content addressing, final validation, score arithmetic under a named "
            "rubric version, and the workflow's own transitions."
        ),
        promised=True,
        evidence="test_the_golden_article_passes_every_check",
    ),
    Guarantee(
        name="linked_replays",
        title="A replay is a new execution, linked to its original",
        detail=(
            "Repeating a stage never overwrites what it repeats. The re-run branches from "
            "the execution it came from, so both remain readable and the relationship "
            "between them is a recorded fact rather than an inference from timestamps."
        ),
        promised=True,
        evidence="test_a_replay_runs_the_stage_again_without_touching_the_original",
    ),
    Guarantee(
        name="original_versus_replay_comparison",
        title="Original and replay can be compared",
        detail=(
            "Two executions can be placed side by side field by field — configuration, "
            "prompt, response, output, cost, latency — with the differences marked, so "
            "what changed between them is answerable without reading two traces."
        ),
        promised=True,
        evidence="test_a_comparison_names_what_differs_between_two_executions",
    ),
)


#: What it does not, said out loud.
NOT_PROMISED: tuple[Guarantee, ...] = (
    Guarantee(
        name="identical_model_output",
        title="Identical output from a hosted model",
        detail=(
            "Not promised, and not achievable. A hosted model may return different prose "
            "for the same request — because of sampling, a silently updated model behind "
            "a stable name, or provider-side changes nobody publishes. A replay therefore "
            "produces a new execution to compare against the original, never a claim that "
            "the two must match."
        ),
        promised=False,
        evidence="test_a_replay_runs_the_stage_again_without_touching_the_original",
    ),
)


def contract() -> tuple[Guarantee, ...]:
    """The whole contract, promises first, as a client is shown it."""
    return REPRODUCIBILITY + NOT_PROMISED


__all__ = ["NOT_PROMISED", "REPRODUCIBILITY", "Guarantee", "contract"]
