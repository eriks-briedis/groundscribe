"""LLM-layer enumerations (phase 04).

Provider-neutral by construction. Every provider spells structured output
differently — a ``response_format`` block, a forced tool call, or nothing but
prompting — and recording each provider's own word for it would make two records
from two providers incomparable. These names describe *how the schema was
enforced*, which is the part a reader of a provenance record actually needs.
"""

from __future__ import annotations

from enum import StrEnum


class StructuredOutputMode(StrEnum):
    """How a structured response was constrained.

    The mode changes what a failure means: an invalid enum under
    ``NATIVE_SCHEMA`` says the provider's own constraint engine let it through,
    while the same failure under ``PROMPTED`` says only that the model ignored an
    instruction. Conflating them would send a debugging session in the wrong
    direction.
    """

    NATIVE_SCHEMA = "native_schema"
    JSON_MODE = "json_mode"
    PROMPTED = "prompted"
    NONE = "none"
