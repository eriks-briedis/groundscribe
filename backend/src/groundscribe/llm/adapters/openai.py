"""OpenAI adapter (stub, phase 04)."""

from __future__ import annotations

from typing import ClassVar

from groundscribe.llm.adapters.base import StubAdapter
from groundscribe.llm.enums import StructuredOutputMode


class OpenAIAdapter(StubAdapter):
    """Talks to the OpenAI Responses/Chat APIs once wired.

    Defaults to native schema enforcement: the provider validates the structured
    output itself, so a schema violation that still gets through says something
    about the provider, not merely about the prompt.
    """

    provider: ClassVar[str] = "openai"
    structured_output_mode: ClassVar[StructuredOutputMode] = StructuredOutputMode.NATIVE_SCHEMA
