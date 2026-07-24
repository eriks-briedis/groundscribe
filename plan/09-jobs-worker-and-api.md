# Phase 09 — Background Jobs, Worker, API & CLI

## Goal

Move LLM work off the HTTP request lifecycle onto a database-backed job queue + separate
worker, expose the workflow as command-style FastAPI endpoints with `available_actions`
and OpenAPI as the contract source of truth, and add a Typer CLI that calls the same
application services (no separate workflow logic).

## Depends on

- Phases 05–08 (engine + all stages), Phase 03 (jobs/provenance).

## Spec references

- *Technical architecture → Background execution, API layer*.
- *API design* (command endpoints, `available_actions`).
- *CLI* (Typer commands, "call the same application services").
- *Failure handling* (worker crashes, resumption, duplicate-job prevention, orphaned
  executions).
- *Domain model → Job*.

## Deliverables

- **Jobs table + worker:** enqueue job → worker claims → runs stage execution → persists
  results/artefacts → emits SSE progress event. Reliable claiming, duplicate-job
  prevention, superseded-job handling, worker restart/resumption, orphaned-execution
  detection; partial execution data preserved on failure.
- **Application service layer:** the single entry point both API and CLI call; issues
  commands to the workflow engine, never re-implements transition rules.
- **FastAPI command endpoints** (per spec list), e.g.: `POST /projects`,
  `POST /projects/{id}/sources`, `POST /projects/{id}/source-model/extract`,
  `POST /projects/{id}/source-gaps/{gap_id}/answer`,
  `POST /projects/{id}/architecture/propose`, `PUT /projects/{id}/architecture/{ver}`,
  `POST /projects/{id}/architecture/{ver}/approve`, `POST /articles/{id}/brief/generate`,
  `.../draft`, `.../review`, `.../revision-plan`, `.../rewrite`, `.../voice-align`,
  `.../score`, `.../validate`, `.../approve`, `POST /executions/{id}/replay`,
  `POST /executions/{id}/fork`, `GET /executions/{id}`, `.../events`, `.../invocations`,
  `GET /executions/compare`, `POST /experiments`, `GET /jobs/{id}/events`.
- **State-driven `available_actions`** in responses (e.g. `revision_required` →
  `approve_revision_plan`, `edit_revision_plan`, `return_to_brief`, `fork_execution`,
  `override_and_approve`).
- **SSE progress** stream per job/execution.
- **OpenAPI generation** as the contract source of truth (consumed by phase 11).
- **Typer CLI** mirroring the spec's commands (`writer project create`, `source import`,
  `source extract`, `architecture propose`, `article draft/review/rewrite/export`,
  `execution inspect/replay/fork`, `experiment compare`), all delegating to the service
  layer.

## Test-first specification

- **Job lifecycle (unit):** claim → run → complete; a claimed job isn't double-claimed;
  duplicate enqueue is prevented; a superseded job is marked, not run twice.
- **Worker crash retains trace (provenance):** killing a worker mid-stage preserves the
  partial `StageExecution` + invocations + trace events; the job is resumable/orphan-
  detectable.
- **API command → transition (integration):** each command endpoint drives the correct
  state transition and returns updated state + `available_actions`.
- **available_actions correctness (unit):** each state returns exactly the spec's valid
  actions.
- **Async enqueue (integration):** long-running commands enqueue a job and return
  immediately (no model call inside the request).
- **SSE (integration):** progress events stream for a running job.
- **OpenAPI generation (contract):** the schema generates and includes all command
  endpoints + response models.
- **CLI ↔ service parity (unit):** a CLI command and its API counterpart invoke the same
  service method with equivalent arguments; the CLI contains no transition logic of its
  own.

## Implementation tasks

1. Implement the jobs table, claiming, dedup, supersession, resumption, orphan detection.
2. Implement the worker process loop + SSE event emission.
3. Build the application service layer over the engine.
4. Implement FastAPI command endpoints + `available_actions` + async enqueue.
5. Configure OpenAPI generation output into `contracts/`.
6. Implement the Typer CLI delegating to the service layer.
7. Make all tests green.

## Exit criteria / spec-conformance checklist

- [ ] LLM work runs in the worker, never in the HTTP request lifecycle.
- [ ] Jobs support reliable claim, dedup, supersession, restart/resume, orphan detection.
- [ ] Partial traces preserved on worker failure.
- [ ] All spec command endpoints implemented and return `available_actions`.
- [ ] SSE progress works per job.
- [ ] OpenAPI schema generated into `contracts/`.
- [ ] CLI mirrors commands and shares the service layer (no duplicated workflow logic).

## Risks & non-goals for this phase

- **Non-goal:** Dramatiq/Celery/Temporal — DB-backed worker only (spec: defer frameworks).
- **Non-goal:** frontend (phase 11), replay/fork *semantics* depth (phase 12) — endpoints
  exist here, richer comparison lands in phase 12.
- **Risk:** API embedding workflow rules — forbidden; all rules stay in the engine.
