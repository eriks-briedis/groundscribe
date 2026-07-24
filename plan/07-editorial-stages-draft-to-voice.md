# Phase 07 — Editorial Stages: Draft → Voice

## Goal

Implement the drafting-through-style half of the pipeline: initial draft generation,
substantive review, revision planning, substantive rewrite, and line-editing / voice
alignment. Enforce the separation of structural editing from stylistic editing, immutable
branching versions, and reviewer-output-as-evidence (not command).

## Depends on

- Phase 06 (source model, architecture, brief, `PipelineStage` protocol).
- Phase 10 provides the full voice system, but this phase consumes an *active voice
  profile* object (a minimal profile is sufficient here; rich learning lands in phase 10).

## Spec references

- *Editorial workflow §7 Initial drafting*, *§8 Substantive review*, *§9 Revision
  planning*, *§10 Substantive rewrite*, *§11 Line editing and voice alignment*.
- *Domain model → Review, ReviewIssue, RevisionPlan, ArticleVersion*.
- *Version lineage* (branching rewrites).

## Deliverables

- **GenerateInitialDraft:** draft from approved source model + locked architecture + brief
  + active voice profile + constraints; does not resolve missing facts — instead omits
  unsupported material, uses qualified language, inserts a visible unresolved marker, or
  requests return to gap analysis. Stored as an immutable `ArticleVersion` with full trace
  (input snapshots, message sequence, context ordering, rendered prompt, params, raw
  response, final text, finish reason, usage/cost).
- **ReviewSubstantively:** argument/accuracy focus (not sentence polish). Review
  dimensions per spec; each `ReviewIssue` carries severity (blocking/major/minor/optional),
  category, article location, passage, description, evidence, related source/brief ref,
  recommended correction, suggested route, blocks-publication flag, reviewer confidence.
  Accepted/rejected findings remain visible across rounds so resolved criticism is not
  reintroduced without evidence.
- **Review acceptance:** user may accept/reject/edit individual findings; reviewer output
  is evidence, not an unquestionable instruction.
- **CreateRevisionPlan:** convert accepted feedback into a coherent plan (accepted/rejected
  findings, required vs optional changes, sections to preserve, claims that must not
  change, sections to remove/move, whether brief/architecture must reopen, expected effect
  on scores); reconciles contradictory findings; stored as a separate immutable artefact
  whose record explains what was combined/deferred/rejected.
- **RewriteSubstantively:** apply the approved revision plan (may change structure, order,
  evidence amount, thesis wording, examples, scope) but must not alter the source model or
  invent facts; creates a new `ArticleVersion` linked to its parent; **multiple rewrites
  may branch from the same parent** (for prompt/model/strategy comparison).
- **AlignVoice:** style-only pass — permitted: rhythm, word choice, flow, repetition,
  formality, mechanical transitions, unnatural phrasing, excessive abstraction, generic AI
  patterns. Prohibited: new claims/examples/technical details, evidence changes, thesis
  changes, removing qualifications, significant structural changes. On discovering a
  structural problem, route back to substantive revision rather than silently changing it.

## Test-first specification

- **Golden — review schema:** representative draft → review with correctly-structured
  issues + severities; schema-level.
- **Severity routing (unit):** blocking/major/minor/optional map to correct suggested
  routes; optional never forces a full iteration.
- **Reviewer-as-evidence (unit):** user accept/reject/edit on findings is honoured; a
  rejected finding stays visible and isn't silently re-raised without new evidence.
- **Revision-plan reconciliation (unit):** contradictory accepted findings are reconciled
  into a coherent plan; the record explains combine/defer/reject decisions.
- **Unresolved-marker handling (unit):** drafting inserts a visible marker / qualifies /
  omits instead of inventing facts; a missing blocking fact can request return to gap
  analysis.
- **Prohibited-change guard (unit/golden):** voice alignment that would add a claim or
  change the thesis is rejected/routed back; permitted stylistic changes pass.
- **Immutable branching lineage (unit):** each rewrite/voice pass is a new immutable
  version; two rewrites branch from one parent; lineage retained.
- **No source mutation (invariant):** rewrite/voice stages never modify the source model.

## Implementation tasks

1. Implement drafting stage + unresolved-fact handling.
2. Implement review stage + issue schema + severity + cross-round issue history.
3. Implement review-acceptance handling (accept/reject/edit → user interventions).
4. Implement revision planner + contradiction reconciliation.
5. Implement substantive rewrite with branch-from-parent lineage.
6. Implement voice-alignment stage with permitted/prohibited enforcement + route-back.
7. Wire all into the state machine + provenance; make tests green.

## Exit criteria / spec-conformance checklist

- [ ] Draft never invents facts; uses omit/qualify/marker/return-to-gaps.
- [ ] Review issues carry the full structured field set + severity + evidence.
- [ ] Accepted/rejected findings persist and remain visible across rounds.
- [ ] Revision plan reconciles contradictions and records combine/defer/reject reasoning.
- [ ] Rewrite applies the plan, never mutates source, and branches from its parent.
- [ ] Voice pass enforces permitted/prohibited changes and routes structural problems back.
- [ ] Every version is immutable with retained lineage.

## Risks & non-goals for this phase

- **Non-goal:** scoring, routing loop, final validation (phase 08).
- **Non-goal:** voice learning/onboarding/precedence (phase 10) — consume a static profile.
- **Risk:** rewriter blindly applying reviewer suggestions — the revision planner exists
  precisely to prevent this; test it explicitly.
