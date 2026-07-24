# groundscribe — Implementation Plan Overview

*Technical Writer Pipeline: a local-first, inspectable editorial workflow system.*

## What this directory is

This `plan/` directory is the executable implementation plan for groundscribe, split
into 14 sequential phases (`01`–`14`). Each phase builds on the previous one and is
delivered **test-first**. Read this file before starting any phase.

## Product summary

groundscribe turns technical source material into focused, accurate, publishable
articles (or article series) through an explicit, versioned editorial state machine.
Its value is not raw generation — it is scope control, factual fidelity, consistent
personal voice, bounded revision loops, and **first-class execution provenance** so any
artefact can be traced back to the exact source, prompts, model calls, tool calls,
scores, routing decisions, and user actions that produced it.

## Guiding principles (hold across every phase)

1. **Source truth is separate from prose.** The structured source model is
   authoritative; generated prose is disposable.
2. **Observable provenance is part of the product**, modelled as structured domain data,
   not application logs. No reliance on hidden model chain-of-thought.
3. **Explicit state machine over autonomous agents.** Transitions are typed, testable,
   and reproducible.
4. **Immutable, branching snapshots over destructive edits.** Nothing is silently
   overwritten; lineage supports multiple successors.
5. **Structured outputs where decisions matter** (routing, scoring, extraction, review);
   free-form text only for article prose.
6. **Human control at high-leverage decisions**, not every sentence.
7. **Local-first by default**, with visible data flow to external providers.

## Tech stack (fixed by spec — do not deviate without user sign-off)

- **Backend:** Python 3.12+, FastAPI, Pydantic, SQLAlchemy, Alembic, Jinja2, Typer.
- **Storage:** SQLite for local dev, PostgreSQL for concurrent/long-term; avoid
  SQLite-specific behaviour so migration stays trivial.
- **Frontend:** React + TypeScript (Vite), OpenAPI-generated client, SSE for progress.
- **Workflow:** custom Python state machine (not LangGraph/Temporal initially).
- **Background work:** DB-backed jobs table + separate Python worker process.
- **Testing:** pytest, pytest-asyncio, Hypothesis, coverage; deterministic fake LLM.

## Repo layout (target)

```
groundscribe/
├── backend/          # Python app: domain, workflow, stages, api, cli, provenance
├── frontend/         # React + TypeScript
├── contracts/        # Generated OpenAPI + TS types (contract source of truth)
├── prompts/          # Versioned Jinja2 prompt templates + metadata.yaml
├── evaluations/      # Golden data, eval datasets, evaluation suite
├── tests/            # Cross-cutting / integration tests
├── docker/
├── compose.yaml
└── README.md
```

## The TDD workflow contract (every phase follows this)

For each unit of behaviour in a phase:

1. **Spec** — restate the exact behaviour/invariant from the source document.
2. **Red** — write the test(s) named in the phase's *Test-first specification* and watch
   them fail. **Commit** them (`test(...): ... (red)`).
3. **Green** — write the minimum implementation to pass. **Commit** (`feat(...)`/`fix(...)`).
4. **Refactor** — clean up with tests green. **Commit** if anything changed (`refactor(...)`).
5. **Conform** — tick the phase's *Exit criteria / spec-conformance checklist*; the phase
   is done only when all tests pass and every box is ticked.

Each red→green→refactor cycle produces its own small, descriptive commits per the
*Version control & commit discipline* rules below — the git history should read as a
step-by-step record of how and why each behaviour was built.

Test categories used throughout (from the spec's Testing strategy):
- **Unit** — domain rules, score math, precedence, redaction.
- **State-machine / property (Hypothesis)** — transition invariants over sequences.
- **LLM-contract** — behaviour against a fake LLM client (valid/invalid/repair/timeout/
  fallback/refusal/tool-call).
- **Golden** — representative source → expected structured output (schema-level, not
  exact prose).
- **Provenance** — reconstructability, linkage, redaction, hash integrity.

## Version control & commit discipline (MANDATORY, all phases)

Remote: `https://github.com/eriks-briedis/groundscribe` (initialised in phase 01).

Provenance is a product principle; the git history is the provenance of the *codebase
itself* and must be held to the same standard. Every phase follows these rules:

- **Commit frequently, at logical boundaries — not in large batches.** A commit should
  capture one coherent step: a red test suite, the implementation that turns it green, a
  refactor, a schema/migration, a config change, or a decision. If a change mixes
  unrelated concerns, split it into separate commits.
- **Never commit a broken tree as a "finished" step.** Intentional red-test commits are
  allowed only when the message says so (see the `test:` convention below); otherwise a
  commit must lint, type-check, and pass its tests.
- **Descriptive messages that explain the change and the decision behind it**, not just
  what changed. Body text should answer *why* when the reason is non-obvious (trade-offs,
  rejected alternatives, spec section satisfied). Reference the phase and the relevant
  spec area, e.g. `[phase 05]` and the invariant/section addressed.
- **Conventional-commit-style prefixes** for scannability:
  `test:` (add failing/red tests), `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`,
  `migrate:` (schema/Alembic), `perf:`. Example sequence within a TDD unit:
  ```
  test(phase-05): add state-machine invariants for rewrite limits (red)
  feat(phase-05): enforce 3/2/1 rewrite limits with approval override
  refactor(phase-05): extract routing-policy resolver to versioned object
  ```
- **One decision per commit for design decisions.** When a phase makes a versioned-policy
  or architectural choice, isolate it in its own commit whose message records the decision
  and why, so the history is a readable trail of *why the system is shaped this way*.
- **Traceability:** a reviewer reading `git log` alone should be able to reconstruct which
  test drove which change, which spec requirement it satisfies, and why each decision was
  made — mirroring the execution-provenance guarantee the product gives its users.
- Do not squash away the red→green→refactor rhythm; that history is the evidence the TDD
  process was actually followed.

## Phase dependency graph

```
01 Foundations & tooling
        │
02 Domain model & snapshots
        │
03 Provenance & execution records
        │
04 LLM provider & prompt management
        │
05 Workflow state machine
        │
        ├────────────────┐
06 Stages: source→brief  │
        │                │
07 Stages: draft→voice   │
        │                │
08 Scoring, routing,     │
   final validation      │
        │                │
09 Jobs, worker & API ───┘
        │
10 Voice personalisation
        │
11 Frontend web app
        │
12 Experimentation, replay & evaluation
        │
13 Security, privacy & export
        │
14 Deployment, CLI packaging & wrap-up
```

Each phase's `Depends on` field lists only earlier phases; the chain is acyclic.

## Global conventions

- **Versioning is first-class.** Prompts, rubrics, schemas, routing policies, voice
  profiles, architectures, and briefs all carry explicit versions and are captured in
  every stage execution record.
- **Immutability + content addressing.** Artefacts are content-hashed `ArtifactSnapshot`s;
  the same input is stored once and referenced.
- **Redaction before persistence.** Secrets/confidential material are removed before any
  trace, prompt, artefact, or log is written — never after.
- **No silent mutation.** Every write that supersedes an artefact creates a new snapshot
  with lineage back to its parent.
- **Every artefact references a creating execution.** Enforced as a state-machine
  invariant (phase 05) and a provenance test (phase 03).

## Definition of done (per phase)

A phase is complete when: all named tests are green; the conformance checklist is fully
ticked; new code is typed (mypy clean) and linted (ruff clean); and nothing in the
phase's *Non-goals* leaked into scope.

## MVP alignment

The spec's product MVP phases map onto these engineering phases:
- **Spec Phase 1 (workflow + provenance core)** → engineering phases 01–09.
- **Spec Phase 2 (usable local web app)** → phase 11 (plus 09).
- **Spec Phase 3 (personalisation + evaluation)** → phases 10 and 12.
- **Spec Phase 4 (experimentation + integrations)** → phases 12–14.
