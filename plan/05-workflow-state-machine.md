# Phase 05 — Workflow State Machine

## Goal

Implement the explicit editorial workflow engine: states, validated transitions, routing
policies, rewrite limits, stagnation detection, human-pause, `available_actions` by state,
and decision-record emission — independent from FastAPI route handlers. This is where the
core "state machine over agents" principle becomes enforceable and testable.

## Depends on

- Phase 03 (decision records, executions).
- Phase 02 (artefact lineage).
- Phase 04 not strictly required for the engine logic itself (stages are stubbed here and
  filled in phases 06–08), but the engine creates stage executions that later call models.

## Spec references

- *Technical architecture → Workflow engine* (responsibilities, example states, rationale).
- *Product principles → Explicit workflow over autonomous agents*.
- *Review and scoring loop → Routing rules, Rewrite limits, Stagnation detection*.
- *Domain model → PipelineRun, StageExecution*.
- *Testing strategy → State-machine tests* (the invariant list).
- *API design* (`available_actions` by state).

## Deliverables

- State enum (spec's ~24 states): `SOURCE_INGESTED`, `SOURCE_MODEL_EXTRACTING`,
  `SOURCE_QUESTIONS_REQUIRED`, `SOURCE_MODEL_READY`, `ARCHITECTURE_PROPOSING`,
  `ARCHITECTURE_REVIEW_REQUIRED`, `ARCHITECTURE_APPROVED`, `BRIEF_GENERATING`,
  `BRIEF_REVIEW_REQUIRED`, `DRAFT_GENERATING`, `SUBSTANTIVE_REVIEWING`,
  `REVISION_PLAN_REQUIRED`, `SUBSTANTIVE_REWRITING`, `VOICE_ALIGNING`, `SCORING`,
  `REVISION_REQUIRED`, `PASSED`, `FINAL_VALIDATING`, `HUMAN_APPROVAL_REQUIRED`,
  `COMPLETED`, `FAILED`, `CANCELLED`, `STALLED`.
- Transition table with guards; illegal transitions rejected.
- **Routing policy** (versioned): factual gap → source extraction / questions;
  architecture issue → architecture / brief; substantive issue → revision planning /
  rewrite; style issue → voice alignment; minor local → targeted patch; pass → final
  validation.
- **Rewrite limits** (versioned defaults): max 3 substantive rewrites, max 2 style-only,
  max 1 automatic architecture reopening; further rounds require explicit user approval.
- **Stagnation detection:** improvement < 2 points for two rounds; same blocking issue
  survives two rewrites; oscillating scores; one dimension improves while another
  deteriorates; latest not measurably better than parent; high manual edit distance after
  repeated voice passes → route to `STALLED` / human decision.
- `available_actions(state)` resolver returning valid next actions.
- Human-pause mechanism (engine parks at review/approval states).
- Every transition emits a `DecisionRecord` naming its triggering policy/actor.
- Enforcement that **every produced artefact references a creating execution** and
  **no approved architecture mutates silently**.

## Test-first specification

State-machine + Hypothesis property tests covering the spec's invariants:

- An article cannot reach `COMPLETED`/export before `FINAL_VALIDATING` passes.
- An approved (`ARCHITECTURE_APPROVED`) architecture cannot change silently — changes
  require a new versioned snapshot + override record.
- A failed factual-fidelity score cannot route only to style editing.
- A style-only failure must not trigger source extraction.
- Rewrite limits cannot be exceeded without user approval.
- Every article version retains lineage.
- Final export must use the version that passed validation.
- Confidential source material cannot appear in publishable output (engine-level guard;
  full enforcement in phase 13).
- Every generated artefact references a creating execution (transition rejected otherwise).
- Replays cannot overwrite original executions (creates a new linked execution).
- Failed executions retain their trace.
- A routing decision must identify its triggering policy or actor.
- **Property (Hypothesis):** for random valid action sequences, the machine never enters
  an illegal state and never exceeds rewrite limits without an approval action.

## Implementation tasks

1. Define the state enum and typed transition table with guards.
2. Implement the routing policy resolver (versioned) mapping failure categories → target
   stage/state.
3. Implement rewrite-limit counters and enforcement per article/branch.
4. Implement stagnation detection over version/score history.
5. Implement `available_actions` resolver.
6. Wire `DecisionRecord` emission into every transition.
7. Add engine-level guards for the artefact-references-execution and no-silent-mutation
   invariants.
8. Make all state-machine and property tests green (stages stubbed with fakes).

## Exit criteria / spec-conformance checklist

- [ ] All spec states + guarded transitions implemented; illegal transitions rejected.
- [ ] Routing policy is versioned and routes each failure class to the correcting stage.
- [ ] Rewrite limits (3/2/1) enforced; extra rounds need explicit approval.
- [ ] Stagnation conditions detected → `STALLED`/human decision.
- [ ] `available_actions(state)` correct for every state.
- [ ] Every transition emits a decision record naming its policy/actor.
- [ ] Every spec "State-machine tests" invariant has a passing test.
- [ ] Hypothesis property test passes over random valid sequences.

## Risks & non-goals for this phase

- **Non-goal:** real stage implementations (phases 06–08) — use fakes/stubs returning
  canned `StageResult`s.
- **Non-goal:** background execution/jobs (phase 09) — the engine runs synchronously in
  tests here.
- **Risk:** encoding routing thresholds inline — keep them in the versioned policy object,
  shared with phase 08.
