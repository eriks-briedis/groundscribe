"""Provider adapters — the only place a provider SDK may ever be imported.

plan/04 ships these as *stubs*: real network calls are an explicit non-goal, and
a stub that returned a plausible answer would be worse than one that raises,
because the tests would then pass on fiction. Each adapter carries real provider
metadata and a real retry policy, so the shape callers depend on is fixed before
any SDK is wired in — the moment an adapter needs a wider surface is the moment
provider concepts start leaking into the callers.
"""

from __future__ import annotations

from groundscribe.llm.adapters.anthropic import AnthropicAdapter
from groundscribe.llm.adapters.base import StubAdapter
from groundscribe.llm.adapters.ollama import OllamaAdapter
from groundscribe.llm.adapters.openai import OpenAIAdapter

__all__ = ["AnthropicAdapter", "OllamaAdapter", "OpenAIAdapter", "StubAdapter"]
