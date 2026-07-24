# Phase 01 — Foundations & Tooling

## Goal

Stand up the monorepo, Python tooling, test harness, database test infrastructure, and a
**deterministic fake LLM client** — so that from this point on every later phase can be
built strictly test-first. This phase writes almost no product logic; its product *is*
the ability to do TDD reliably.

## Depends on

- None (first phase).

## Spec references

- *Technical architecture* (high-level architecture, Python backend rationale).
- *Testing strategy* (all subsections — this phase makes each type runnable).
- *Deployment → Local development* (monorepo layout).
- *LLM contract tests* (fake LLM clients).

## Deliverables

- **Git repository initialised** (the project is not yet under version control) with a
  `.gitignore`, an initial commit, and the *Version control & commit discipline* rules
  from `00-overview.md` adopted from the first commit onward. A `CONTRIBUTING.md` (or
  README section) restates the commit conventions so the rule is enforced going forward.
- Optionally a commit-message lint hook (e.g. via pre-commit / commitlint) enforcing the
  conventional-commit prefixes.
- Monorepo skeleton per `00-overview.md` layout (`backend/`, `frontend/` placeholder,
  `contracts/`, `prompts/`, `evaluations/`, `tests/`, `docker/`, `compose.yaml`,
  `README.md`).
- Backend package `groundscribe` with dependency + tooling config (uv or poetry, ruff,
  mypy, pytest, pytest-asyncio, Hypothesis, coverage, pre-commit).
- **Deterministic fake LLM client** implementing the same interface later defined fully
  in phase 04: scripted responses keyed by call, plus injectable failures (invalid
  schema, invalid enum, timeout, provider error, rate limit, refusal, tool call,
  fallback trigger).
- Database test harness: SQLAlchemy engine factory, in-memory SQLite fixture,
  transactional test isolation, Alembic initialised with an empty baseline migration.
- Test fixtures package (`tests/fixtures/`) and shared pytest `conftest.py`.
- CI workflow running lint + type-check + tests + coverage gate.

## Test-first specification

Write these before the corresponding tooling exists:

- **Harness self-tests (unit):**
  - In-memory DB fixture creates/tears down a schema and rolls back between tests.
  - Engine factory produces a working session; a trivial model round-trips.
- **Alembic migration tests:**
  - `upgrade head` then `downgrade base` succeeds on a scratch SQLite DB.
  - Baseline migration is empty/no-op and reversible.
- **Fake LLM client contract (LLM-contract):**
  - Returns scripted structured output for a given call key.
  - Can be scripted to raise each injectable failure type on demand.
  - Records the effective request it received (so provenance tests can assert on it).
  - Is deterministic: identical scripting → identical outputs.
- **Tooling smoke:**
  - `ruff`, `mypy`, and `pytest` run clean on the empty skeleton.

## Implementation tasks

0. `git init`, add `.gitignore`, wire the remote
   `https://github.com/eriks-briedis/groundscribe`, and make the initial commit. Adopt the
   commit-discipline rules from `00-overview.md` immediately; document them in
   `CONTRIBUTING.md`. Every subsequent task in every phase is committed per those rules.
1. Create the monorepo directories and root config files.
2. Initialise the Python backend project + lockfile; pin Python 3.12+.
3. Configure ruff, mypy (strict), pytest (+asyncio mode), coverage threshold, pre-commit.
4. Write the DB test harness and Alembic baseline; make the migration tests pass.
5. Implement the fake LLM client and its recording/failure-injection behaviour; make the
   contract tests pass.
6. Add CI running lint → type-check → tests with coverage gate.
7. Write `README.md` bootstrap instructions (local run, tests).

## Exit criteria / spec-conformance checklist

- [ ] Git repo initialised, remote `https://github.com/eriks-briedis/groundscribe` set,
      commit discipline documented and in force from the first commit.
- [ ] Monorepo layout matches `00-overview.md`.
- [ ] `pytest`, `ruff`, `mypy` all green on CI.
- [ ] In-memory DB fixture isolates tests transactionally.
- [ ] Alembic `upgrade`/`downgrade` round-trips.
- [ ] Fake LLM client supports all injectable failure types and records effective
      requests deterministically.
- [ ] Coverage gate enforced in CI.

## Risks & non-goals for this phase

- **Non-goal:** any domain models, real provider adapters, API routes, or frontend code.
- **Non-goal:** Postgres wiring beyond "avoid SQLite-only features" (real Postgres run is
  phase 14).
- **Risk:** over-building the fake client. Keep it to the interface needed by phase 04;
  expand it there if required.
