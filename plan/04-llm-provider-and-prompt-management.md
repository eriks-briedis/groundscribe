# Phase 04 — LLM Provider Abstraction & Prompt Management

## Goal

Provide a narrow internal LLM interface (so provider SDK types never leak into the domain),
the structured-output validation + repair ladder, per-stage model routing config, and a
versioned Jinja2 prompt system. Every model call must emit the provenance records defined
in phase 03.

## Depends on

- Phase 03 (provenance records to populate).
- Phase 01 (fake LLM client — now formalised behind the real interface).

## Spec references

- *LLM and tool integration → Provider abstraction, Model routing, Prompt management,
  Structured outputs*.
- *Execution provenance → Exact effective request, Runtime configuration, Validation and
  repair attempts*.
- *Structured outputs* repair sequence.
- *LLM contract tests* (Testing strategy).

## Deliverables

- `LLMClient` protocol: structured generation, text generation, streaming, tool calling,
  token/cost reporting, retry policy, provider metadata. Real client is out of scope for
  tests but adapters are stubbed for: OpenAI, Anthropic, Ollama / OpenAI-compatible local.
  The phase-01 fake implements this protocol.
- **Repair ladder** for invalid structured output: (1) retry with validation feedback,
  (2) constrained repair prompt, (3) configured fallback model, (4) fail stage → human
  intervention. Every attempt recorded as an ordered child `ModelInvocation` (phase 03).
- **Prompt store** under `prompts/<stage>/vN.jinja2` + `metadata.yaml`, with a renderer
  that captures template id, version, input variables, rendered prompt, effective message
  sequence, and output-schema version into the effective-request record.
- **Per-stage model-routing config** (versioned): which provider/model/params each stage
  uses (e.g. extraction = strong structured model; drafting = prose model; validation =
  cheap/deterministic), overridable and captured per execution.
- Runtime-configuration capture: provider, exact model id, revision, temperature, top-p,
  seed, max output tokens, reasoning effort, structured-output mode, tool choice, stop
  sequences, API version, client-library version, timeout, retry policy.

## Test-first specification

(LLM-contract tests, all against the fake client.)

- **Valid structured output:** parses and validates against the Pydantic schema; one
  accepted invocation recorded.
- **Invalid JSON → repair:** first attempt unparseable, repair prompt issued, second
  attempt accepted; both recorded in order with correct attempt types.
- **Invalid enum → repair:** structurally valid JSON but bad enum value triggers a repair
  attempt with validation feedback.
- **Fallback model:** repeated failures on the primary escalate to the configured fallback
  model, recorded as a `model-fallback` attempt.
- **Timeout / provider error / rate limit:** each surfaces as its typed retry and, on
  exhaustion, fails the stage rather than silently returning garbage.
- **Refusal:** provider refusal is captured (refusal state) and routed to human
  intervention, not treated as a valid result.
- **Tool call:** a model-requested tool call is captured as a `ToolInvocation` with
  `initiated_by = model`.
- **Prompt render + version capture:** rendering a template records its version, inputs,
  rendered text, and message sequence into the effective request; changing the template
  version changes the recorded version.
- **Model routing:** each stage resolves to its configured model; an override is captured
  in the execution record.

## Implementation tasks

1. Define the `LLMClient` protocol + provider metadata and stub adapters.
2. Implement structured-generation with Pydantic validation.
3. Implement the repair ladder and wire each attempt to phase-03 invocation chaining.
4. Implement the Jinja2 prompt store, renderer, and `metadata.yaml` version loading.
5. Implement per-stage model routing config with versioning + override capture.
6. Implement runtime-config capture on every invocation.
7. Make all LLM-contract tests green.

## Exit criteria / spec-conformance checklist

- [ ] Provider SDK types do not appear in the domain layer.
- [ ] Structured outputs validated by Pydantic; invalid output never accepted silently.
- [ ] Repair ladder implemented end-to-end with each attempt recorded and typed.
- [ ] Prompts are versioned files; rendered prompt + version captured per execution.
- [ ] Per-stage model routing configurable and versioned.
- [ ] Full runtime configuration captured on every model invocation.
- [ ] Refusals/timeouts/rate-limits handled distinctly and inspectably.

## Risks & non-goals for this phase

- **Non-goal:** real network calls to providers in tests (fakes only); real adapters may
  be smoke-tested manually but are not gated.
- **Non-goal:** stage business logic (phases 06–08).
- **Risk:** embedding prompts as strings in code — forbidden; all prompts live under
  `prompts/` as versioned files.
