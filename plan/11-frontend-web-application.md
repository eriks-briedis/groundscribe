# Phase 11 — Frontend Web Application

## Goal

Build the local-first, artefact-first React + TypeScript web application on top of the
generated OpenAPI client: the core screens, SSE progress, diff/score-history/lineage
visualisations, and execution/trace inspection. The frontend displays backend state and
submits commands; it never re-implements pipeline-transition rules.

## Depends on

- Phase 09 (API, OpenAPI contract, SSE, `available_actions`). Consumes all prior phases'
  artefacts read-only.

## Spec references

- *Product interface* (recommended form, artefact-first, core screens).
- *Frontend* (technologies, responsibilities).
- *Review history*, *Execution timeline*, *Stage inspector*, *Lineage graph*, *Run
  comparison*, *Trace filters*.

## Deliverables

- React + TypeScript app (Vite), TS types + client **generated from OpenAPI** (Orval /
  openapi-typescript / hey-api), Markdown editor + preview, text diff viewer, timeline +
  graph visualisations, structured forms for source/review artefacts, SSE consumption.
- **Core screens:**
  - *Project dashboard* — status, source completeness, proposed articles, current stage,
    active jobs, unresolved questions, revision counts, approval state, recent failures,
    token/cost summaries.
  - *Source workspace* — sources, extracted facts, claims, evidence, unknowns,
    confidential material, provenance links, provider-visibility rules.
  - *Question queue* — blocking + high-value questions, reason each matters, answer status,
    unknown/confidential options, resulting source-model changes.
  - *Architecture board* — article-concept cards with merge/split/delete/reorder/rename/
    edit-thesis/reassign-evidence/approve/compare-versions.
  - *Article workspace* — brief, current version, source evidence, reviewer findings,
    revision plan, voice rules, previous version, diff, scores, available actions,
    producing execution, branch lineage.
  - *Review history* — score progression table + issue history (resolved/reopened/new,
    disagreements, stagnation warnings, rubric/reviewer versions, confidence).
  - *Execution timeline* — chronological expandable trace events.
  - *Stage inspector* — summary, inputs, context selection, effective request, raw
    response, parsed result, tool calls, validation attempts, decisions, outputs,
    cost/timing, errors.
  - *Lineage graph* — branching causal relationships between artefacts.
  - *Run comparison* — side-by-side config/prompt/context/response/output/score/cost/
    latency/preference/edit-distance diffs.
  - *Trace filters* — failed executions, schema repairs, fallback models, blocking
    findings, user overrides, high-cost calls, low-confidence scores, confidential-data
    warnings, repeated unresolved issues.
- Human-approval view surfacing everything required before approval (final article,
  scores, confidence, remaining concerns, rewrite rounds, lineage, diff, validation
  results, model/prompt versions, interventions, cost/usage, full trace) with actions:
  approve / manual edit / targeted revision / override score / export / fork / abandon.
- **Progressive disclosure**: summary views by default, expandable raw payloads; separate
  editorial vs debugging modes (trace-overload mitigation).

## Test-first specification

- **Generated-client contract tests:** components consume the generated client; a contract
  test fails if the OpenAPI schema drifts from what the UI expects.
- **available_actions rendering (component):** action buttons render exactly the backend's
  `available_actions`; the UI never invents an action.
- **SSE rendering (component):** progress events update the timeline/dashboard live.
- **Diff + score-history rendering (component):** version diffs and the score-progression
  table render from backend data.
- **No client-side transition rules (unit/lint):** a guard test asserts the frontend has no
  independent transition/routing logic (actions come only from backend state).
- **Artefact-first (component):** primary views render artefacts (not a chat transcript).

## Implementation tasks

1. Scaffold the Vite React/TS app + OpenAPI client generation into `contracts/`.
2. Build shared primitives: diff viewer, timeline, lineage graph, score table, forms.
3. Build each core screen against the generated client.
4. Wire SSE progress + `available_actions`-driven action bars.
5. Implement progressive disclosure + editorial/debug modes.
6. Make component + contract tests green.

## Exit criteria / spec-conformance checklist

- [ ] TS client generated from OpenAPI; drift is caught by contract tests.
- [ ] All core screens implemented and artefact-first (not chatbot).
- [ ] Actions rendered strictly from backend `available_actions`.
- [ ] SSE progress, diffs, score history, lineage graph, run comparison, trace filters
      all functional.
- [ ] Human-approval view surfaces the full required context.
- [ ] Progressive disclosure + editorial/debug modes implemented.
- [ ] No transition/routing logic in the frontend.

## Risks & non-goals for this phase

- **Non-goal:** real-time collaborative editing (explicit product non-goal).
- **Non-goal:** publishing integrations, Tauri packaging (phase 13/14).
- **Risk:** trace overload — enforce progressive disclosure + filters + summary defaults.
