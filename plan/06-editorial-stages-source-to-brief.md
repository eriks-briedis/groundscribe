# Phase 06 — Editorial Stages: Source → Brief

## Goal

Implement the first half of the editorial pipeline as concrete `PipelineStage`s: source
ingestion + source-of-truth extraction, gap analysis & questioning, content-architecture
proposal, user architecture override, and article-brief generation. Each stage is
schema-constrained, records full provenance, and produces immutable artefacts.

## Depends on

- Phase 04 (LLM + prompts), Phase 05 (state machine), Phase 03 (provenance), Phase 02
  (artefacts).

## Spec references

- *Editorial workflow §1 Source ingestion*, *§2 Source-of-truth extraction*,
  *§3 Gap analysis and user questioning*, *§4 Content architecture*,
  *§5 User architecture override*, *§6 Article brief generation*.
- *Stage interface* (`PipelineStage` protocol, per-execution record fields).
- *Golden tests* (Testing strategy).

## Deliverables

- `PipelineStage` protocol + `PipelineContext` + `StageResult` types.
- **Source ingestion:** import Markdown / plain text / pasted notes; store immutable
  `SourceDocument`, parsed `SourceSegment`s, content hashes, project constraints
  (audience, platform, depth, confidential names, length, first-person allowed, allowed
  providers, trace-retention consent), confidentiality/provider-access flags.
- **ExtractSourceTruth:** structured source model (product facts, development history,
  classified claims+evidence, publication constraints, lessons/potential arguments);
  records schema version, rendered prompt, included/excluded segments + reasons, token
  budget/truncation, raw+parsed response, validation failures, repairs, final accepted
  model, config.
- **GenerateGapQuestions:** produce prioritised gaps (blocking / high-value / optional);
  only blocking + selected high-value surface automatically; each question states why it
  matters; queue interface supports answer / skip / unknown / confidential / defer /
  "premise incorrect"; answers regenerate/amend the source model with a visible diff and
  full answer provenance.
- **ProposeContentArchitecture:** analyse distinct arguments, evidence per argument,
  overlap, standalone-ability, reader knowledge, platform constraints, competing theses,
  thin-content risk; output proposed article model(s) + series-level considerations +
  structured decision record (selected architecture, supporting claims, alternatives +
  why rejected, confidence, uncertainties, policy version).
- **User architecture override:** merge/split/remove/reorder/rename/edit-thesis/reassign-
  evidence; surface trade-off warnings without blocking; architecture locking (versioned,
  cannot silently change); override provenance (before/after snapshot, structured diff,
  reason, warnings shown/accepted, lineage branch).
- **GenerateArticleBrief:** per approved article, a brief-as-contract (title, thesis,
  audience, reader knowledge, reader problem, opening direction, argument structure,
  evidence per section, required examples, claims requiring qualification, required
  conclusion, length, platform constraints, active voice profile, style overrides,
  excluded material, reserved material, definition-of-done); distinguishes mandatory vs
  optional.

## Test-first specification

- **Golden — extraction:** representative source → expected structured source-model schema
  (claims classified, evidence linked); schema-level match, not exact prose.
- **Golden — architecture:** representative source model → expected architecture schema
  (article concepts, decision record); asserts alternatives-considered recorded.
- **Golden — brief:** approved concept → brief schema with all required fields +
  definition-of-done.
- **Gap prioritisation (unit):** only blocking + selected high-value surface; optional
  suppressed; each surfaced question carries a reason.
- **Answer provenance (provenance):** each `UserAnswer` retains original question, reason,
  addressed gaps, exact answer, classification, resulting source-model diff, and creating
  execution.
- **Override provenance & diff (provenance):** an override produces before/after snapshots,
  a structured diff, records warnings shown/accepted, and creates a lineage branch; the
  approved architecture is locked afterward.
- **Stage execution metadata (provenance):** each stage records the full spec field set
  (stage name, input/output snapshot ids, prompt/rubric/schema versions, model + params,
  usage, cost, time, retries, tool calls, routing decision, stage impl version).
- **LLM-contract:** extraction/architecture handle invalid-schema → repair correctly
  (reuses phase 04 ladder).

## Implementation tasks

1. Define `PipelineStage` protocol + context/result types.
2. Implement source ingestion + parsing + constraint capture.
3. Implement each stage's prompt templates (versioned) + Pydantic output schemas.
4. Implement extraction, gap generation + question queue + answer application.
5. Implement architecture proposal + decision record.
6. Implement override handling (diff, warnings, locking, branch).
7. Implement brief generation with definition-of-done.
8. Wire each stage into the state machine and provenance; make golden + unit + provenance
   tests green.

## Exit criteria / spec-conformance checklist

- [ ] Source ingested immutably with segments, hashes, constraints, confidentiality flags.
- [ ] Extraction produces a classified, evidence-linked source model with full trace.
- [ ] Gaps prioritised; only blocking + selected high-value surface; each states why.
- [ ] Question queue supports all six response types; answers amend model with visible
      diff + provenance.
- [ ] Architecture proposal records alternatives-considered and confidence.
- [ ] Override produces diff + branch, surfaces (non-blocking) warnings, and locks the
      approved architecture.
- [ ] Brief contains every required field + definition-of-done, mandatory vs optional
      distinguished.
- [ ] Golden tests for extraction/architecture/brief pass at schema level.

## Risks & non-goals for this phase

- **Non-goal:** drafting/review/scoring (phases 07–08); frontend queue UI (phase 11).
- **Non-goal:** retrieval — small projects use direct source inclusion; retrieval is
  phase 12.
- **Risk:** over-questioning — enforce prioritisation and grouping per spec risk mitigation.
