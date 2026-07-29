# Known issues

Defects found while building, judged real, and deliberately not fixed yet — each
because fixing it needs a decision rather than a patch, or because it is a
papercut that should not interrupt the phase that found it.

Everything here has been reproduced. Where there is a measurement, the command
that produced it is written down so the next person does not have to trust this
file.

---

## 1. A running job holds the database for its whole run

**Status:** open. **Found:** phase 11, wiring the one-command dev stack.
**Severity:** high with a real provider attached; invisible without one.

`Worker.run_once` claims the job — a write, so under `BEGIN IMMEDIATE` it takes
SQLite's write lock — then `await handler(...)` runs the entire stage, model
calls included, and only afterwards commits. The lock is held for the whole span.

Reproduced with a stage suspended mid-run, against a real file on disk:

```
while the stage is running, another process can write: False
after the job commits, another process can write:  True
```

With the fake LLM client the window is milliseconds, which is why the suite and
the live smoke tests are all green. With a real provider it is however long the
model takes, and during it every API write waits out `BUSY_TIMEOUT_MS` (15s) and
then fails. In practice: the dashboard stops accepting anything exactly while the
pipeline is doing the interesting work.

`BEGIN IMMEDIATE` (see `backend/src/groundscribe/db.py`) widened the window — the
lock is now taken at claim rather than at the stage's first write — but the span
was always the whole job.

**Why it is not fixed:** the fix contradicts a decision phase 03 made on purpose.
`Worker._commit` states it: *"one job, one transaction — the unit a worker can
retry is the unit that must either have happened or not"*. Shortening the
transaction means deciding what a half-run job looks like when a worker dies
mid-stage, which is a change to the recovery model, not a tweak.

**Options:**

- *Commit around the model call.* Record provenance up to the request, release
  the connection, call the model, re-open to record the response. Keeps SQLite
  usable under a slow provider; costs the all-or-nothing property the recovery
  logic relies on, so orphan detection and replay both need re-examining.
- *Leave it, and treat Postgres as the answer whenever a real provider is
  attached* — which is what plan/00 already says about concurrency. No risk, but
  the local-first default stays fragile precisely when it is working hardest.

**Where:** `backend/src/groundscribe/jobs/worker.py` (`run_once`, `_commit`),
`backend/src/groundscribe/db.py` (`_install_sqlite_transaction_control`).

---

## 2. Alembic ignores `GROUNDSCRIBE_DATABASE_URL`

**Status:** open. **Found:** phase 11, pointing a smoke test at its own database.
**Severity:** low, but it wastes an afternoon the first time.

The application reads `GROUNDSCRIBE_DATABASE_URL`
(`backend/src/groundscribe/app/bootstrap.py`); Alembic reads `sqlalchemy.url`
from `alembic.ini`. Point the app at a custom database and `alembic upgrade head`
migrates a *different* file, with no error — the application then fails at
runtime with `no such table: projects`, which names neither cause.

**Fix:** have `backend/alembic/env.py` prefer the environment variable, falling
back to the ini. Two lines and a test that the resolver prefers the environment.

---

## 3. A lock timeout is reported as a 500

**Status:** open. **Found:** phase 11, under concurrent writes.
**Severity:** low on its own; it is mostly what makes issue 1 unpleasant.

When a write waits out the busy timeout, SQLAlchemy's `OperationalError` reaches
the client as a generic `500 Internal Server Error`. "The database is busy, try
again" is a `503` with a `Retry-After`: a client can act on that, and a person
reading it learns something true. The status map in
`backend/src/groundscribe/api/app.py` already draws exactly this kind of
distinction for domain failures; this one is missing from it.

---

## 4. The weak-rubric flag has nowhere to fire from

**Status:** open. **Found:** phase 12, wiring the manual edit distance.
**Severity:** low today; it is the one thing plan/12 asks for that is built
and unreachable.

`experiments/edit_distance.rubric_signal` implements plan/12's *high score +
heavy editing → weak rubric*. It needs two facts about one article: what the
rubric scored it, and how far the version a person published sits from the
version the pipeline proposed. Nothing in the system produces that pair.

A forked stage produces one or the other and never both — a voice pass
writes an article and no score, a scoring pass writes a score and no
article — so an experiment cannot assemble it whichever stage its corpus
points at. The pair exists in exactly one place: a person editing an
approved article by hand.

**Why it is not fixed:** phase 10 built `VoiceLearning.record_edit` for
exactly that and never exposed it, which is the same absence recorded
below as "no Markdown editor". One endpoint accepting a hand-edited
version closes both, and it is that phase's work rather than phase 12's.

**What does work meanwhile:** the distance itself is a metric on every
experiment arm, measured against the article the author approved
(`experiments/metrics.ArmMetrics.manual_edit_distance`). It is the
*rubric* reading of the number that is waiting.

**Where:** `backend/src/groundscribe/experiments/edit_distance.py`
(`rubric_signal`), `backend/src/groundscribe/voice/learning.py`
(`record_edit`).

---

## Not issues, recorded so they are not rediscovered as bugs

- **`writer worker run` drains the queue once and exits.** Deliberate (a crash
  halfway through a batch keeps what the finished jobs did). A long-lived worker
  polls on top of it; `scripts/dev.sh` is that loop. A supervised worker is
  phase 14's.
- **No Markdown *editor*, only a preview.** No endpoint accepts a manually edited
  version, so the editor would be a text box with nowhere to save to. It arrives
  with the endpoint.
- **The approval view's *manual edit*, *targeted revision* and *export* actions
  are absent.** Same reason: no backend command exists for them yet; export is
  phase 13's.
- **Accessing the dev server by hostname needs `server.allowedHosts`.** Vite's
  DNS-rebinding guard, not a defect. IP addresses always work.
- **The `confidential_warning` trace filter reads stored payloads.** It is the
  only evidence redaction leaves, so the cost is inherent; it is a filter a
  person asks for and never part of the default listing.
