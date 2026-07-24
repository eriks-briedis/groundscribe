"""LLM client interfaces and the deterministic test fake.

Phase 01 ships only what the test harness needs (plan/01 Risk: do not over-build
the fake — the full provider/prompt interface arrives in phase 04). Everything
here is re-exported from :mod:`groundscribe.llm.fake`.
"""

from __future__ import annotations

from groundscribe.llm.fake import (
    FakeLLMClient,
    InjectableFailure,
    InjectedFailureError,
    LLMRequest,
    LLMResponse,
    LLMScriptError,
)

__all__ = [
    "FakeLLMClient",
    "InjectableFailure",
    "InjectedFailureError",
    "LLMRequest",
    "LLMResponse",
    "LLMScriptError",
]
