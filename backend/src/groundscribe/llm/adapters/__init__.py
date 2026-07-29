"""Provider adapters — the only place a provider SDK may ever be imported.

plan/04 shipped these as *stubs*: real network calls were an explicit non-goal,
and a stub that returned a plausible answer would be worse than one that raises,
because the tests would then pass on fiction. Each adapter carries real provider
metadata and a real retry policy, so the shape callers depend on was fixed before
anything was wired in — the moment an adapter needs a wider surface is the moment
provider concepts start leaking into the callers.

:class:`OpenAIClient` is no longer a stub. It is the one provider this project
actually calls, and it is the proof the stub shape was the right one: wiring it
needed no change to the protocol, to the generator, or to any caller.
"""

from __future__ import annotations

from groundscribe.llm.adapters.anthropic import AnthropicAdapter
from groundscribe.llm.adapters.base import StubAdapter
from groundscribe.llm.adapters.ollama import OllamaAdapter
from groundscribe.llm.adapters.openai import MissingAPIKey, OpenAIClient

__all__ = [
    "AnthropicAdapter",
    "MissingAPIKey",
    "OllamaAdapter",
    "OpenAIClient",
    "StubAdapter",
]
