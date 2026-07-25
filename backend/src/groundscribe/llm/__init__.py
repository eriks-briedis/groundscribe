"""LLM client interfaces, provider adapters and the deterministic test fake.

Phase 04 puts the narrow :class:`~groundscribe.llm.protocol.LLMClient` protocol at
the centre: callers import from here, never from a provider SDK. Adapters live in
:mod:`groundscribe.llm.adapters` — the one package permitted to know a provider
exists.
"""

from __future__ import annotations

from groundscribe.llm.enums import StructuredOutputMode
from groundscribe.llm.errors import (
    LLMError,
    LLMNetworkError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from groundscribe.llm.fake import (
    FakeLLMClient,
    InjectableFailure,
    InjectedFailureError,
    LLMScriptError,
)
from groundscribe.llm.protocol import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    ProviderMetadata,
    RetryPolicy,
    RuntimeConfig,
    StreamChunk,
    TokenUsage,
    ToolCall,
)

__all__ = [
    "FakeLLMClient",
    "InjectableFailure",
    "InjectedFailureError",
    "LLMClient",
    "LLMError",
    "LLMNetworkError",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMRequest",
    "LLMResponse",
    "LLMScriptError",
    "LLMTimeoutError",
    "ProviderMetadata",
    "RetryPolicy",
    "RuntimeConfig",
    "StreamChunk",
    "StructuredOutputMode",
    "TokenUsage",
    "ToolCall",
]
