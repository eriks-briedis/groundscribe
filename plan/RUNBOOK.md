# Runbook — Implementing groundscribe phase by phase

Paste the prompt below to instruct Claude (Claude Code) to pick the next phase from
`plan/` and implement it under all the rules defined in `plan/00-overview.md`. Use it once
per phase; it self-detects where the project is and stops at the end of each phase for your
go-ahead.

---

```
You are implementing the groundscribe project. The full implementation plan lives in
./plan/ as an overview plus 14 sequential, TDD-driven phase docs (00–14).

FIRST, orient yourself:
1. Read plan/00-overview.md in full — it defines the principles, the TDD workflow
   contract, the phase dependency graph, and the MANDATORY version-control & commit
   discipline rules. These rules govern everything you do.
2. Determine the CURRENT STATE of the repo: inspect git history, the file tree, and which
   tests exist/pass. Identify the lowest-numbered phase (plan/NN-*.md) that is not yet
   complete — i.e. whose "Exit criteria / spec-conformance checklist" is not fully
   satisfied. That is THE NEXT PHASE.
3. Before starting, confirm every phase listed in the next phase's "Depends on" is
   actually complete. If a dependency is unmet, stop and tell me — do not skip ahead or
   build out of order.

THEN, tell me which phase you selected and give me a short summary of what it delivers and
the test-first plan you'll follow. Wait for nothing further unless a dependency is unmet or
a genuine decision is blocked — otherwise proceed to implement it.

IMPLEMENT the phase strictly by its doc, obeying ALL rules from plan/00-overview.md:
- Work test-first: for each behaviour in the phase's "Test-first specification", write the
  test(s) and watch them fail (RED) before writing implementation (GREEN), then REFACTOR.
- Do NOT write implementation code before its failing test exists.
- Stay within the phase's scope; respect its "Risks & non-goals" — do not pull work forward
  from later phases.
- Do not deviate from the spec's fixed tech stack or the "explicit state machine over
  agents" decision.
- Keep everything typed (mypy clean) and linted (ruff clean).

COMMIT DISCIPLINE (from plan/00-overview.md — follow exactly):
- Commit frequently at logical boundaries: red tests, the implementation that greens them,
  refactors, migrations, config, and design decisions each as their own coherent commit.
  No large mixed batches.
- Use conventional-commit prefixes (test:/feat:/fix:/refactor:/chore:/docs:/migrate:/perf:)
  scoped with the phase, e.g. `feat(phase-05): ...`.
- Write descriptive messages explaining the change AND the decision/why behind it,
  referencing the phase and the spec requirement/invariant satisfied.
- Isolate each design decision in its own commit whose message records the decision and
  rationale. Never commit a broken tree as a finished step (an intentional red-test commit
  is fine only if the message marks it "(red)").
- Preserve the red→green→refactor rhythm in history; don't squash it away.

FINISH the phase only when:
- Every test named in the phase's Test-first specification passes; the whole suite is green.
- ruff + mypy are clean.
- Every box in the phase's "Exit criteria / spec-conformance checklist" is genuinely ticked.
- All work is committed per the rules above and pushed to the remote
  (https://github.com/eriks-briedis/groundscribe).

Then report: the phase completed, the checklist status, the tests added, and the key
commits — and stop. Do not start the next phase without my go-ahead.
```

---

## Notes

- Phase 01 is the entry point (it does `git init`, wires the remote, and adopts the commit
  rules). Run the prompt for the first time and it will select phase 01.
- The prompt is intentionally stateless: each run re-detects the next incomplete phase, so
  you can use the same text every time regardless of which session or machine you're on.
- To force a specific phase instead of auto-detect, append a line such as:
  `Override: implement plan/07-editorial-stages-draft-to-voice.md regardless of detection.`
