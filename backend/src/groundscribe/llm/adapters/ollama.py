"""Ollama / OpenAI-compatible local adapter (stub, phase 04)."""

from __future__ import annotations

from typing import ClassVar

from groundscribe.llm.adapters.base import StubAdapter
from groundscribe.llm.enums import StructuredOutputMode


class OllamaAdapter(StubAdapter):
    """Talks to a local Ollama or other OpenAI-compatible endpoint once wired.

    Defaults to JSON mode: local models generally accept a "respond with JSON"
    constraint but enforce no schema, so invalid-enum failures are expected here
    and the repair ladder — not the provider — is what catches them. This is the
    adapter that keeps the product's local-first promise honest.
    """

    provider: ClassVar[str] = "ollama"
    structured_output_mode: ClassVar[StructuredOutputMode] = StructuredOutputMode.JSON_MODE
