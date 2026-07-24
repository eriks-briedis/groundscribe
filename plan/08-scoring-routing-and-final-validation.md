# Phase 08 — Scoring, Routing Loop & Final Validation

## Goal

Implement the evaluation/scoring stage, the bounded revision loop that routes failures to
the correcting stage, and the deterministic final-validation stage. This closes the
editorial loop: score → route or pass → validate → human approval (phase 09/11).

## Depends on

- Phase 07 (article versions to score), Phase 05 (routing policy, limits, stagnation),
  Phase 03 (evaluation runs), Phase 04 (reviewer model calls).

## Spec references

- *Review and scoring loop* (scoring dimensions, overall score, passing conditions,
  evaluation provenance, evidence-backed deductions, score confidence/instability,
  editorial-score-vs-routing, routing rules, rewrite limits, stagnation detection,
  reviewer consistency).
- *Final validation* (validation checks, allowed actions).
- *Domain model → EvaluationRun, ValidationReport*.

## Deliverables

- **ScoreArticle stage:** weighted scoring across dimensions (factual fidelity 25%, thesis
  & focus 15%, structure & coherence 15%, evidence & specificity 15%, reader value 10%,
  scope discipline 10%, voice adherence 10%) with **configurable, versioned weights per
  content type**. Overall = weighted combination on a 0–100 scale (explicitly not an
  objective measurement).
- **Passing policy (versioned):** overall ≥ 85, factual fidelity ≥ 90, thesis & focus ≥ 80,
  scope discipline ≥ 80, voice adherence ≥ 75, no blocking issues, no unsupported major
  claims. High scores in other dimensions must not mask a critical weakness.
- **Editorial-score vs routing-result separation:** an artefact may score 87 yet route
  `fail` due to an unsupported major claim; the interface shows both.
- **Evidence-backed deductions:** every material deduction records dimension, passage,
  source/brief requirement, mismatch explanation, severity, recommended route, confidence.
  Optional stylistic preferences don't force a rewrite unless the rubric marks them
  required.
- **Score confidence/instability:** optional repeated or multi-model scoring for
  high-stakes final reviews; report repeat scores + dispersion.
- **Routing integration:** on fail, route to the correcting stage (factual → source /
  questions; architecture → architecture / brief; substantive → revision planning /
  rewrite; style → voice; minor → targeted patch; pass → final validation), emitting a
  `DecisionRecord`. Enforce rewrite limits + stagnation escalation (approve-despite-score,
  add source, narrow thesis, reopen brief/architecture, lower threshold, authorise
  rewrite, abandon).
- **Evaluation provenance:** every score links to exact article/source-model/brief/voice-
  profile/rubric/anchor versions, reviewer prompt+model+params, raw+parsed response,
  validation/repair attempts, parent comparison, threshold policy, routing decision.
- **ValidateFinalOutput stage (deterministic / tightly constrained):** no confidential
  names; no prohibited terminology; no unresolved placeholders; required facts present;
  no unsupported numbers introduced; title matches thesis; formatting matches platform;
  length within range; valid Markdown; valid links/references; no reserved-material leak;
  exported version == version that passed review; artefact matches recorded content hash;
  no trace-only/source-only annotations remain. May pass, apply safe mechanical
  corrections, produce a validation failure, or route to the relevant earlier stage — but
  never creatively rewrite.

## Test-first specification

- **Score math (unit):** weighted overall computed correctly; changing content-type weights
  changes the result; weight set is versioned and captured.
- **Threshold policy (unit):** passing requires all conditions; a below-threshold single
  dimension fails even with a high overall.
- **Hard-failure not masked (unit):** high non-fidelity scores cannot lift a failing
  factual fidelity to a pass.
- **Editorial vs routing (unit):** a case with good overall but a blocking claim yields
  `verdict=fail` while showing the numeric score.
- **Evidence-backed deduction (provenance):** each material deduction carries its full
  evidence record; optional-preference deductions don't trigger a rewrite.
- **Routing (state-machine):** each failure class routes to the correct stage with a
  decision record (ties to phase 05 tests).
- **Stagnation escalation (unit):** stagnation conditions surface the escalation options
  and require a human decision.
- **Score confidence (unit):** repeated scoring reports repeat_scores + dispersion.
- **Validation rules (unit, one test each):** every validation check above, plus the
  safe-mechanical-correction path and the exported==passed + content-hash-match checks.
- **Evaluation provenance (provenance):** a score with missing linkage is not treated as
  valid historical data.

## Implementation tasks

1. Implement dimension scoring + versioned weight/threshold policies.
2. Implement evidence-backed deduction records + confidence/dispersion.
3. Integrate scoring result with the phase-05 routing policy, limits, and stagnation.
4. Implement final validation checks as deterministic functions + safe-correction path.
5. Wire evaluation provenance linkage; make all tests green.

## Exit criteria / spec-conformance checklist

- [ ] Weighted scoring with configurable, versioned, content-type weights.
- [ ] Passing policy enforces all conditions; hard failures can't be masked.
- [ ] Editorial score and routing result are separate and both surfaced.
- [ ] Every material deduction is evidence-backed; optional prefs don't force rewrites.
- [ ] Failing scores route to the correcting stage with a decision record.
- [ ] Rewrite limits + stagnation escalation enforced.
- [ ] Optional repeated/multi-model scoring reports dispersion.
- [ ] All final-validation checks implemented, including exported==passed + hash match.
- [ ] Evaluation provenance fully linked.

## Risks & non-goals for this phase

- **Non-goal:** human approval UI + export (phase 09/11/13).
- **Non-goal:** experiment-level metric aggregation (phase 12).
- **Risk:** false score precision — surface evidence/confidence/trends, never present
  scores as objective measurements (spec risk mitigation).
