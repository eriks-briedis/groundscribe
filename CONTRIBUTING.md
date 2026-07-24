# Contributing to groundscribe

groundscribe treats **execution provenance as part of the product**. The git history is
the provenance of the codebase itself and is held to the same standard: a reviewer reading
`git log` alone should be able to reconstruct *which test drove which change, which spec
requirement it satisfies, and why each decision was made*.

These rules are in force from the first commit (see `plan/00-overview.md` §"Version control
& commit discipline").

## Test-first workflow (TDD contract)

For each unit of behaviour:

1. **Spec** — restate the exact behaviour/invariant being implemented.
2. **Red** — write the test(s) and watch them fail. Commit them with a `(red)` marker.
3. **Green** — write the minimum implementation to pass. Commit.
4. **Refactor** — clean up with tests green. Commit if anything changed.
5. **Conform** — tick the phase's exit-criteria checklist.

Do **not** write implementation code before its failing test exists. Do not squash away the
red→green→refactor rhythm.

## Commit discipline (mandatory)

- **Commit frequently, at logical boundaries — not in large batches.** One coherent step
  per commit: a red test suite, the implementation that greens it, a refactor, a
  schema/migration, a config change, or a design decision.
- **Never commit a broken tree as a "finished" step.** A commit must lint, type-check, and
  pass its tests — *unless* it is an intentional red-test commit, which must say `(red)` in
  the subject.
- **One design decision per commit.** Isolate versioned-policy/architectural choices in
  their own commit whose message records the decision and the rationale.
- **Descriptive messages that explain the change and the why**, not just the what.
  Reference the phase and the relevant spec area, e.g. `[phase 01]`.

## Conventional-commit prefixes

`test:` (add failing/red tests), `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`,
`migrate:` (schema/Alembic), `perf:`, `ci:`. Scope with the phase, e.g.:

```
test(phase-01): add fake-LLM contract tests for failure injection (red)
feat(phase-01): implement scripted fake LLM client with failure injection
refactor(phase-01): extract response-scripting into keyed registry
```

## Quality gates (run before every non-red commit)

```
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

All four must be clean. CI enforces the same plus a coverage gate.
