# Known issues

Defects found while building, judged real, and deliberately not fixed yet — each
because fixing it needs a decision rather than a patch, or because it is a
papercut that should not interrupt the phase that found it.

Everything here has been reproduced. Where there is a measurement, the command
that produced it is written down so the next person does not have to trust this
file.

---

## 1. A running job holds the database for its whole run

**Status:** open **for writes**; reads were fixed in phase 15. **Found:** phase
11, wiring the one-command dev stack. **Severity:** was high with a real provider
attached — the whole application went dead for the length of a model call. Now
low: everything a person *looks at* works while a job runs, and only commands
queue behind it.

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
model takes.

### What was fixed: reads never needed the lock

Write-ahead logging entitles a reader to proceed against the last committed
snapshot while a writer works. This project was opting out of that: `BEGIN
IMMEDIATE` was emitted for *every* transaction, so a `GET` that wrote nothing
still queued behind the worker, waited out `BUSY_TIMEOUT_MS`, and failed. Every
screen in the application is a `GET`.

A transaction can now say it will only read (`db.read_only`, a
`groundscribe_read_only` execution option honoured in the `begin` handler), and
gets a deferred `BEGIN` for saying so. The API's read side and the job event
stream take it; commands do not, because they read before they write and that is
the interleaving `IMMEDIATE` exists to protect. The promise is unenforced — a
"read-only" transaction that writes gets the snapshot-upgrade refusal, loudly, at
the write — which is why it is given only to the projection layer, whose contract
already is that a read changes nothing (`backend/tests/test_db_concurrency.py`,
`backend/tests/test_api.py::test_a_screen_still_answers_while_a_job_holds_the_database`).

### What is still open: two writers, one file

A command issued while a job runs still waits out the busy timeout and then
fails — as a `503` now (§3), not a `500`. In practice that is cancelling a run
mid-stage, or working on a second project while the first is generating.

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
- *Treat Postgres as the answer whenever a real provider is attached* — which is
  what plan/00 already says about concurrency, and what `compose.yaml`'s
  `postgres` profile is for. No risk, no code, and the local-first default stays
  as it is: correct, and single-writer.

**Where:** `backend/src/groundscribe/jobs/worker.py` (`run_once`, `_commit`),
`backend/src/groundscribe/db.py` (`_install_sqlite_transaction_control`,
`read_only`).

---

## 2. Alembic ignores `GROUNDSCRIBE_DATABASE_URL`

**Status:** **fixed in phase 14.** **Found:** phase 11, pointing a smoke test at
its own database. **Severity when open:** low, but it wasted an afternoon.

The application reads `GROUNDSCRIBE_DATABASE_URL`
(`backend/src/groundscribe/app/bootstrap.py`); Alembic read `sqlalchemy.url`
from `alembic.ini`. Point the app at a custom database and `alembic upgrade head`
migrated a *different* file, with no error — the application then failed at
runtime with `no such table: projects`, which names neither cause.

It stopped being a papercut the moment phase 14 built the container stack, where
the two are *always* different: the entrypoint migrates and the API reads, and
the first run reported twenty-one successful upgrades followed by a 500 on the
first command.

`backend/alembic/env.py` now prefers the environment variable, falls back to the
ini, and treats whitespace as unset (`backend/tests/test_migration_target.py`).

---

## 3. A lock timeout is reported as a 500

**Status:** **fixed in phase 15.** **Found:** phase 11, under concurrent writes.
**Severity when open:** low on its own; it is mostly what made issue 1
unpleasant.

When a write waited out the busy timeout, SQLAlchemy's `OperationalError` reached
the client as a generic `500 Internal Server Error`. "The database is busy, try
again" is a `503` with a `Retry-After`: a client can act on that, and a person
reading it learns something true.

`backend/src/groundscribe/api/app.py` now maps contention — matched on the
driver's own words, so it covers PostgreSQL's lock timeout and deadlock as well
as SQLite's locked database — to a `503` carrying `Retry-After` and a sentence
saying nothing was written. Anything else wearing `OperationalError` is re-raised
unchanged: a missing table does not clear while a person waits, and telling them
to retry would hide the only useful thing about it.

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

## 5. A source segment's offsets and the stored document can disagree

**Status:** open. **Found:** phase 13, auditing the store for a leaked secret.
**Severity:** low, and only for source material that actually contains a
secret.

`SourceSegment.char_start` / `char_end` index the document *as pasted*, and
`content_hash` is the hash of the segment's own unredacted text. The document
snapshot beside them goes through the recorder, so it is stored **redacted**
(`ProvenanceRecorder.record_text_output`). When redaction changes the length of
anything — replacing `sk-live-…` with `[REDACTED:api_key]` does — slicing the
stored document by a later segment's offsets no longer yields that segment.

Reproduced by ingesting a three-block document whose middle block contains
`api_key=sk-live-…`, then slicing the stored snapshot by each segment's own
offsets:

```
seg 0: match=True   'We shipped a read-through cache in March.'
seg 1: match=False  'The deploy used api_key=[REDACTED:api_key] here.\n\np99 latency '
seg 2: match=False  'll from 810ms to 120ms.\n'
```

The placeholder is shorter than the key it replaced, so every segment after it
slides by the difference. A document with no secret in it is unaffected, which
is why this has gone unnoticed: it needs redaction to have actually fired.

The rows themselves are consistent, and every citation the pipeline makes is
checked against the segment rows rather than by slicing the snapshot, so nothing
in the system currently reads the two together. It is the *verifiability* claim
that is weakened: phase 06 says a citation is checkable by slicing the offsets
out of the stored bytes and comparing, and for a document containing a secret
that check now fails on material that is perfectly correct.

**Why it is not fixed:** the two defensible fixes point in opposite directions
and the choice is a product decision, not a patch.

- *Record offsets into the redacted document.* Keeps the slice-and-compare check
  true; means the offsets no longer describe what the author pasted, so a person
  looking at their own file finds them wrong.
- *Store the source snapshot unredacted*, treating it as the author's material
  like the segment rows already are. Keeps offsets honest and moves a secret into
  the content-addressed store, which is the thing exports and backups carry.

**Where:** `backend/src/groundscribe/stages/ingestion.py` (`_store_segments`),
`backend/src/groundscribe/provenance/recorder.py` (`record_text_output`),
`backend/tests/test_secret_audit.py`.

---

## 6. `StageExecution.ordinal` is never assigned

**Status:** open. **Found:** phase 14, running the whole suite against
PostgreSQL. **Severity:** low now that the ordering is fixed; it was the cause of
a real defect before that.

Nothing ever passes `ordinal` to `ProvenanceRecorder.start_stage`, so every stage
execution in the database carries the default `0`. The column is dead weight
carrying a name that promises otherwise.

It mattered because `PipelineRun.stage_executions` was ordered by it alone.
A constant sort key means the database decides, SQLite decides insertion order
and looks correct, and PostgreSQL reorders a run's history — which surfaced as
`test_the_failed_pass_keeps_its_trace` asserting on the wrong "last" stage, and
would have surfaced in the trace view, the stage inspector and the dashboard.
The relationship now sorts by `ordinal, started_at, id`, which is total whatever
`ordinal` holds.

**Why the column is not simply removed or populated:** populating it means
deciding what the number should *mean* — position within the run, or within the
run including the replays and forks that branch off it, which phase 12 makes a
live question. Removing it means a migration and losing a natural place for that
answer. Both are phase-03 decisions rather than phase-14 ones, and neither is
urgent now that nothing depends on the column for ordering.

**Where:** `backend/src/groundscribe/provenance/models.py` (`stage_executions`),
`backend/src/groundscribe/provenance/recorder.py` (`start_stage`).

---

## 7. An action the table permits is offered without asking whether it can work

**Status:** open, two instances fixed and the class unfixed. **Found:** phase 16,
by pressing the buttons. **Severity:** medium — every instance is a control that
either errors or does damage, and the interface presents them as ordinary.

`available_actions` comes from the transition table, which knows which edges are
*legal* from a state and nothing about whether the run has what the edge needs.
The interface renders that list. So an action is offered whenever the machine
would permit it, which is not the same question as whether it can succeed.

Three instances, all real:

- **`approve_revision_plan` before a plan exists.** `revision_plan_required`
  means "a plan is expected here", so the approval edge is legal from the moment
  the run arrives. Approving nothing moved the run to `substantive_rewriting`,
  where the rewrite failed for want of the plan it had been told was approved.
  *Fixed* — the command refuses and names the undecided findings.
- **`decide_finding` on a finding already decided.** *Fixed* — the link is
  withheld once the ledger holds a decision.
- **`abandon_proposal` on a run with no approved architecture.** Offered as a
  primary button while a first proposal is still being generated. Clicking it
  discards work in flight, and the command then refuses it anyway, because there
  is nothing to fall back to. *Open.*

The two fixes are both guards in the service. That stops the damage and leaves
the button, so a person still presses something that answers with an error. The
class needs the *link* withheld the way `decide_finding`'s now is: the read knows
what the run has produced, and it is the only layer that knows both that and
what the table permits.

**Where:** `backend/src/groundscribe/app/actions.py` (`ACTION_ENDPOINTS`),
`backend/src/groundscribe/app/reads.py` (`_action_links`).

---

## 8. "Questions waiting for you" counts gaps nobody was asked

**Status:** open on the dashboard; fixed on the question queue. **Found:** phase
16, on a live run. **Severity:** low, and persistently confusing.

Extraction finds more gaps than it asks about: the policy caps how many are
*surfaced* per round, and records the rest so the run proceeds knowing what it
does not know. The dashboard's link counts every unresolved gap and labels them
"waiting for you".

Observed with the run parked in `architecture_proposing` — nothing waiting on
anybody — offering "9 questions waiting for you", where the true count of
surfaced, unresolved questions was zero. Reproduced:

```
gaps total: 18   surfaced: 1   resolved: 9   surfaced and unresolved: 0
```

The question queue screen was fixed to group asked, answered and merely-noticed
separately. The dashboard count was not, and it is the one a person sees first.

**Where:** `backend/src/groundscribe/app/reads.py` (`dashboard`, the `questions`
list and `source.unresolved_questions`).

---

## 9. The suite's own CI job has been red on `main`

**Status:** open. **Found:** phase 16, while pushing. **Severity:** high — not
because the test matters, but because a permanently red gate is not a gate.

`test_trace_storage.py::test_the_report_and_the_cost_are_both_reachable` asserts
`--report` appears in the CLI's help output. In CI the help renders without it,
so the job that runs the test suite fails on every push. It has been failing on
`main` for some time.

It passes locally at 80 and at 200 columns, so terminal width is not the cause;
the likely difference is that CI has no TTY and the help panel renders
differently. The assertion reads a rendered Rich panel rather than asking the
CLI what options it defines.

Two other jobs are also red — `compose · builds and comes up` and `parity ·
postgres`, both on `tests/test_deployment.py`, where the containerised API never
answers `/health` within 240s. Those are deployment issues; this one is not, and
it is the one that makes every other result unreadable, because a suite that is
always red cannot tell anyone that something broke.

**Where:** `backend/tests/test_trace_storage.py`.

---

## 10. A project's constraints are read with two different orderings

**Status:** open, latent. **Found:** phase 16, reading the code. **Severity:**
low today, medium the moment constraints branch.

`rehydrate.constraints_row` takes the *first* row by `id` ascending;
`advance.auto_advance_enabled` takes the *first* by `id` descending. The ids are
uuid4 hex, so neither ordering means anything, and the two disagree.

With one constraints row per project — which is every project today — both
return the same row and nothing is wrong. The moment a project has two, "what
are this project's constraints" and "is auto-advance on for this project" can be
answered from different rows, and nothing would look wrong from either side.

Constraints are meant to be versioned rather than edited, so a second row is a
designed-for state, not a corruption. `IMPROVEMENTS.md` §1 would add a reason to
create one.

**Where:** `backend/src/groundscribe/app/rehydrate.py` (`constraints_row`),
`backend/src/groundscribe/app/advance.py` (`auto_advance_enabled`).

---

## Not issues, recorded so they are not rediscovered as bugs

- **`writer worker run` drains the queue once and exits.** Deliberate (a crash
  halfway through a batch keeps what the finished jobs did). A long-lived worker
  polls on top of it: `scripts/dev.sh` is that loop locally, and
  `docker/entrypoint.sh worker` is the supervised one, restarted by Compose.
- **No Tauri desktop build.** Deferred on the spec's own guidance — wrap the
  workflow once it is validated, not while it is still moving. The README says
  what is already in place for it and what remains.
- **No Markdown *editor*, only a preview.** No endpoint accepts a manually edited
  version, so the editor would be a text box with nowhere to save to. It arrives
  with the endpoint.
- **The approval view's *manual edit* and *targeted revision* actions are
  absent.** Same reason: no backend command exists for them yet. *Export* now
  does — `GET /versions/{id}/export` and `writer article render` — so the button
  is a phase-11 screen away rather than a missing capability.
- **Compression of stored payloads is not implemented.** The store already
  deduplicates by content address and the payloads are small JSON documents, so
  a compression layer would change the on-disk format for a saving nobody has
  measured. `writer privacy report` is what would produce that measurement;
  revisit when it says something (`backend/src/groundscribe/privacy/storage.py`).
- **Deleting a project's traces leaves shared blobs alone.** Two projects that
  sent the same request share one content-addressed blob, so deletion clears the
  references and deletes only the snapshots nothing else resolves to. The count
  of survivors is returned rather than hidden (`TraceDeletion.shared_payloads`).
- **Accessing the dev server by hostname needs `server.allowedHosts`.** Vite's
  DNS-rebinding guard, not a defect. IP addresses always work.
- **The `confidential_warning` trace filter reads stored payloads.** It is the
  only evidence redaction leaves, so the cost is inherent; it is a filter a
  person asks for and never part of the default listing.
