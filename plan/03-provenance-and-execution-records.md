# Phase 03 — Provenance & Execution Records

## Goal

Build the first-class execution-provenance substrate that explains how every artefact was
produced: pipeline runs, stage executions, model/tool invocations, context selection,
decision records, evaluations, user interventions, and an append-only trace-event stream.
Includes the redaction-before-persistence hook. This is a distinct subsystem from
editorial artefacts and from operational logs.

## Depends on

- Phase 02 (snapshots + editorial entities to reference).

## Spec references

- *Execution provenance system* (entire section: hierarchy, exact effective request,
  context-selection record, raw/parsed/validated responses, validation & repair attempts,
  tool calls, runtime configuration, decision records, user interventions).
- *Domain model → Execution and provenance entities*.
- *Provenance tests* (Testing strategy).
- *Security and privacy → Secret management* (redaction before persistence).
- *Failure handling* (partial data preserved).

## Deliverables

Entities (Pydantic + SQLAlchemy + migrations):

- `PipelineRun`, `StageExecution`, `ContextSelection`, `ModelInvocation` (with
  `parent_invocation_id` for retry/repair chains), `ToolInvocation`, `DecisionRecord`,
  `EvaluationRun`, `UserIntervention`, `TraceEvent` (append-only: `event_type, timestamp,
  actor_type, actor_id, payload, schema_version, correlation_id, causation_id`),
  `ExperimentRun` (shell; filled in phase 12), `Job` (shell; filled in phase 09).
- Provenance hierarchy wiring: `PipelineRun → StageExecution → {InputSnapshot,
  ContextSelection, ModelInvocation{EffectiveRequest, RawResponse, ParsedResponse,
  ValidationAttempts}, ToolInvocation, DecisionRecord, EvaluationRun, UserIntervention,
  OutputArtifact, TraceEvents}`.
- Separation of the three record categories: **editorial artefacts** (phase 02),
  **execution records** (this phase), **evaluation data** (this phase + phase 12) — linked
  but not stored as one unstructured event stream.
- **Redaction hook** applied to every payload before it is persisted (prompts, responses,
  tool args, trace payloads).
- Effective-request model capturing: template id + version, rendered prompt, full message
  sequence with roles, tool definitions supplied, structured-output schema, provider-
  specific request config, and a redacted form.
- Raw / parsed / validated responses stored **separately** as distinct snapshots.

## Test-first specification

(These are the spec's *Provenance tests*.)

- **Effective request reconstructable:** from stored records, the exact request sent to a
  model can be rebuilt (template version + rendered prompt + message sequence + schema).
- **Raw ↔ parsed ↔ validated linkage:** each stays linked; a response that is useful but
  fails schema validation is preserved alongside its repaired successor.
- **Retry ordering:** validation/repair attempts are ordered child invocations
  (`Attempt 1 invalid JSON → Attempt 2 invalid enum → Attempt 3 accepted`), not a bare
  count; retry *types* are distinguishable (network, rate-limit, provider-error,
  invalid-schema, content-repair, model-fallback, manual, prompt-modified).
- **Tool invocations retain args + results:** name, version, invocation id, raw +
  normalised args/results, timing, initiator (model-selected vs pipeline-mandated),
  approval requirement, and which later artefacts depended on the result.
- **Redaction before persistence:** secrets/confidential markers never appear in any
  persisted record; redaction runs pre-write (a test injects a secret and asserts it is
  absent from the stored payload but the record still exists).
- **Snapshot hashes detect mutation:** re-uses phase 02 integrity check at the provenance
  boundary.
- **Branch comparison references correct parent:** a comparison of two executions
  references each side's true parent.
- **Decision records name a policy or actor:** no decision may be stored without
  `decided_by` and (for policy decisions) `policy_version`.
- **Failed executions retain their trace:** simulated failure/cancellation preserves
  partial `StageExecution`, invocations, and trace events.

## Implementation tasks

1. Model the provenance entities + hierarchy relationships; migrations.
2. Implement the effective-request capture + redacted-form generation.
3. Implement invocation chaining (`parent_invocation_id`) and typed retry/attempt records.
4. Implement the append-only `TraceEvent` writer with correlation/causation ids.
5. Implement the redaction hook and wire it into the single persistence path all records
   pass through.
6. Implement context-selection records (candidate/selected/excluded/truncated + strategy
   version).
7. Make all provenance tests green.

## Exit criteria / spec-conformance checklist

- [ ] Full provenance hierarchy modelled and linked.
- [ ] Raw, parsed, and validated responses stored separately and linked.
- [ ] Retry/repair attempts are ordered, typed child invocations.
- [ ] Effective request is reconstructable from stored data.
- [ ] Tool invocations retain args, results, initiator, approval, and downstream deps.
- [ ] Redaction runs before every persistence write; no secret reaches storage.
- [ ] Every decision record names its policy/actor.
- [ ] Failed/cancelled executions retain partial traces.
- [ ] Editorial / execution / evaluation records are separated, not one event blob.

## Risks & non-goals for this phase

- **Non-goal:** actually invoking models (phase 04) or running stages (phase 06+).
  Provenance is exercised here with the fake client + synthetic records.
- **Non-goal:** retention modes, encryption, sanitised export (phase 13) — only the
  redaction-before-persistence guarantee is required now.
- **Risk:** trace storage growth — record the compression/dedup *hooks* but defer tuning
  to phase 13.
