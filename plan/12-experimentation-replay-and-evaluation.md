# Phase 12 — Experimentation, Replay & Evaluation

## Goal

Turn the provenance substrate into a measurable improvement system: inspect / replay /
fork semantics, controlled experiment runs comparing configurations, evaluation datasets
built from approved runs, run comparison metrics, and the manual-edit-distance signal.
Also add retrieval + context-selection inspection for large source collections when needed.

## Depends on

- Phase 03 (provenance, ExperimentRun shell), Phase 08 (scores/metrics), Phase 09
  (replay/fork endpoints), Phase 11 (run-comparison UI).

## Spec references

- *Execution provenance → Inspect, replay, and fork; Reproducibility limits*.
- *Experimentation and pipeline improvement* (experiment model, metrics, evaluation
  datasets, manual edit distance).
- *Storage → Search and retrieval* (retrieval only when source exceeds prompt limits;
  candidate/selected/excluded traceable).

## Deliverables

- **Inspect / Replay / Fork:**
  - *Inspect*: display exactly what happened in the original run (fully supported).
  - *Replay*: re-execute a stage with recorded inputs + config; because hosted models are
    nondeterministic, replay creates a **new linked execution**, never overwriting the
    original.
  - *Fork*: start from an existing execution but alter one or more variables (prompt
    version, model, temperature, voice profile, rubric, source model, context-selection
    strategy, revision plan) — the primary improvement mechanism.
- **Reproducibility contract:** promise complete inspection + config preservation +
  repeatable deterministic operations + linked replays + original-vs-replay comparison;
  do **not** promise bit-for-bit reproduction of hosted models.
- **ExperimentRun:** baseline vs one+ candidate configurations over an evaluation dataset;
  per-example results, aggregate comparison, human-preference decisions.
- **Experiment metrics:** pass rate, human preference, unsupported-claim rate, average
  revision rounds, cost, latency, schema-failure rate, reviewer disagreement, manual-edit
  distance, final acceptance rate, stagnation frequency, confidentiality-validation
  failures.
- **Evaluation datasets:** built from approved historical runs; entries reference immutable
  snapshots (not mutable project state); sensitive projects excluded unless explicitly
  approved.
- **Manual edit distance:** difference between the pipeline's proposed final article and
  the user-approved article (character-level, sentence add/remove, structural changes,
  claim changes, voice corrections) as a quality signal (high score + heavy editing → weak
  rubric).
- **Retrieval (conditional):** full-text/embedding/hybrid source-segment retrieval, added
  only when source collections exceed practical prompt limits; once present, all candidate
  / selected / excluded segments are traceable via the phase-03 context-selection record.

## Test-first specification

- **Inspect fidelity (provenance):** inspection reproduces the original run's records
  exactly.
- **Replay never overwrites (state-machine/provenance):** a replay creates a new execution
  linked to the original; the original remains intact and comparable.
- **Fork variable isolation (unit):** forking changes only the specified variable(s);
  everything else is inherited from the source execution and captured.
- **Dataset references snapshots (unit):** evaluation-dataset entries reference immutable
  snapshots; mutating project state afterward does not change the dataset; sensitive
  projects are excluded unless approved.
- **Metric aggregation (unit):** experiment aggregates compute each metric correctly over
  per-example results.
- **Manual-edit-distance (unit):** distance computed across the specified measures; a
  high-score + high-distance case is flagged.
- **Retrieval traceability (provenance):** when retrieval is used, candidate/selected/
  excluded segments + scores are all recorded.

## Implementation tasks

1. Implement inspect/replay/fork on top of the phase-09 endpoints with linked-execution
   semantics.
2. Implement ExperimentRun execution + per-example + aggregate results.
3. Implement experiment metric computations.
4. Implement evaluation-dataset construction from approved runs (snapshot refs + exclusion).
5. Implement manual-edit-distance measures.
6. Implement conditional retrieval + context-selection tracing.
7. Surface comparisons in the phase-11 run-comparison UI; make tests green.

## Exit criteria / spec-conformance checklist

- [ ] Inspect / replay / fork implemented with correct linked-execution semantics.
- [ ] Reproducibility is not over-promised for hosted models.
- [ ] ExperimentRun compares baseline vs candidates with per-example + aggregate results.
- [ ] All listed experiment metrics computed.
- [ ] Evaluation datasets reference immutable snapshots and exclude sensitive projects.
- [ ] Manual-edit-distance computed and used as a signal.
- [ ] Retrieval (when enabled) keeps candidate/selected/excluded segments traceable.

## Risks & non-goals for this phase

- **Non-goal:** cross-project regression dashboards beyond basic aggregation (later).
- **Risk:** adding retrieval "because it's common" — add only when source size demands it
  (spec guidance).
- **Risk:** misleading reproducibility claims — present replay as a new execution.
