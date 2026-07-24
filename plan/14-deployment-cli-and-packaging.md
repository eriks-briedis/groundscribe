# Phase 14 — Deployment, Packaging & End-to-End Wrap-up

## Goal

Make groundscribe runnable as a local-first application: Docker Compose stack, single
install path, SQLite→Postgres parity, an end-to-end smoke run wiring every stage, the
observability metrics surface, and confirmed failure-handling guarantees. This phase
validates that the whole pipeline works together, not just each part in isolation.

## Depends on

- All prior phases (this is the integration + delivery phase).

## Spec references

- *Deployment* (local development, local distribution, later Tauri).
- *Observability* (metrics, correlated structured logs).
- *Failure handling* (preserved partial data across the whole system).
- *Storage → Database choice* (SQLite vs PostgreSQL; avoid SQLite-specific behaviour).

## Deliverables

- **Docker Compose stack:** `frontend` (:3000), `backend` (:8000), `worker` (separate
  process), `database` (PostgreSQL; SQLite option for lightweight local), `storage` (local
  artefact directory).
- **Single install path:** Docker Compose + a one-command install/bootstrap script; README
  covering local run, tests, and switching SQLite↔Postgres.
- **SQLite→Postgres parity:** the domain avoids SQLite-specific behaviour; the full test
  suite (or a designated integration subset) runs green against Postgres too.
- **End-to-end smoke run:** a full pipeline happy-path on the deterministic fake LLM —
  ingest → extract → gap questions → architecture → approve → brief → draft → review →
  revision plan → rewrite → voice → score → validate → human approval → export — asserting
  a complete, inspectable provenance trace exists at the end.
- **Observability surface:** the spec's metrics (stage duration, token usage, estimated
  cost, retry count, validation failures, schema-repair frequency, score change, rewrite
  count, accepted/rejected issues, stagnation frequency, override frequency, question
  response rate, model fallback frequency, context truncation frequency, tool failure
  frequency, human edit distance, final approval rate) exposed; structured logs correlate
  project / article / pipeline run / stage execution / job / model request / tool
  invocation / trace event.
- **Failure-handling verification:** cancelled stages, worker crashes, timeouts, provider
  refusals, invalid output, partial streaming, tool failures, validation failures,
  user-aborted executions, superseded jobs, orphaned executions — all preserve partial
  data and remain inspectable.
- **Later-packaging note (not built now):** Tauri desktop wrapper deferred until the
  workflow is validated (spec guidance); documented as a follow-up.

## Test-first specification

- **Compose smoke (integration):** `compose up` brings the stack healthy; API + worker +
  DB reachable; a basic command round-trips.
- **End-to-end pipeline (integration):** the full happy-path run on the fake LLM completes
  to export and leaves a complete provenance trace (every stage has a `StageExecution`,
  every artefact references a creating execution).
- **Postgres parity (integration):** the designated integration suite passes against
  Postgres with identical behaviour to SQLite.
- **Observability (unit/integration):** each listed metric is computed/exposed; a sample
  log entry carries all correlation ids.
- **Failure preservation (integration):** each failure class above leaves partial data
  intact and inspectable (extends phase 03/09 guarantees to the assembled system).

## Implementation tasks

1. Write `compose.yaml` + `docker/` images for frontend/backend/worker/db/storage.
2. Write the install/bootstrap script + README run/test instructions.
3. Add the Postgres integration test run to CI alongside SQLite.
4. Implement the end-to-end smoke test on the fake LLM.
5. Wire the observability metrics surface + correlated structured logging.
6. Add system-level failure-preservation integration tests.
7. Document the deferred Tauri packaging path.

## Exit criteria / spec-conformance checklist

- [ ] `compose up` runs the full local stack.
- [ ] Single install path + README documented.
- [ ] Full suite / integration subset green on both SQLite and Postgres.
- [ ] End-to-end pipeline smoke run completes to export with a complete provenance trace.
- [ ] All observability metrics exposed; logs carry correlation ids.
- [ ] Every failure class preserves inspectable partial data.
- [ ] Tauri packaging documented as a deferred follow-up.

## Risks & non-goals for this phase

- **Non-goal now:** Tauri packaging, publishing integrations (X/LinkedIn/Ghost/WordPress/
  Substack/Git/Obsidian), folder-watching — all deferred per spec MVP Phase 4.
- **Non-goal:** distributed workflow frameworks (Temporal etc.) — introduce only if
  parallelism/durability needs emerge after validation.
- **Risk:** infrastructure complexity dominating the project — keep to SQLite/simple
  Postgres + DB-backed worker (spec risk mitigation).
