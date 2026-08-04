"""Provider adapters — the only place a provider SDK may ever be imported.

plan/04 shipped these as *stubs*: real network calls were an explicit non-goal,
and a stub that returned a plausible answer would be worse than one that raises,
because the tests would then pass on fiction. Each adapter carries real provider
metadata and a real retry policy, so the shape callers depend on was fixed before
anything was wired in — the moment an adapter needs a wider surface is the moment
provider concepts start leaking into the callers.

:class:`OpenAIClient` and :class:`OllamaClient` are no longer stubs, and between
them they are the proof the stub shape was the right one: wiring either needed no
change to the protocol, to the generator, or to any caller. The one thing that had
to be added — ``context_window`` — was not a provider concept leaking upward but a
genuinely missing one, because a hosted API allocates the window for you and a
locally hosted model does not.

They are also deliberately *not* built on a shared HTTP base class. Ollama serves
an OpenAI-compatible endpoint, and subclassing across it was measured and
rejected: the compatibility layer ignores ``max_completion_tokens`` outright, so
the shared code would have silently dropped every output budget in the routing
policy. Two wire formats, honestly written twice.
"""

from __future__ import annotations

from groundscribe.llm.adapters.anthropic import AnthropicAdapter
from groundscribe.llm.adapters.base import StubAdapter
from groundscribe.llm.adapters.ollama import OllamaClient
from groundscribe.llm.adapters.openai import MissingAPIKey, OpenAIClient

__all__ = [
    "AnthropicAdapter",
    "MissingAPIKey",
    "OllamaClient",
    "OpenAIClient",
    "StubAdapter",
]
