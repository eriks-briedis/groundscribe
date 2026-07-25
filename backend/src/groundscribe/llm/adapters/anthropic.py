"""Anthropic adapter (stub, phase 04)."""

from __future__ import annotations

from typing import ClassVar

from groundscribe.llm.adapters.base import StubAdapter
from groundscribe.llm.enums import StructuredOutputMode


class AnthropicAdapter(StubAdapter):
    """Talks to the Anthropic Messages API once wired.

    Structured output is obtained by forcing a tool call rather than by a
    dedicated response-format switch, which is why the mode is recorded per
    invocation instead of assumed globally.
    """

    provider: ClassVar[str] = "anthropic"
    structured_output_mode: ClassVar[StructuredOutputMode] = StructuredOutputMode.NATIVE_SCHEMA
