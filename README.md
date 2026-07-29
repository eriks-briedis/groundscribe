# groundscribe

*Technical Writer Pipeline — a local-first, inspectable editorial workflow system.*

groundscribe turns technical source material into focused, accurate, publishable articles
through an explicit, versioned editorial **state machine** with first-class execution
provenance: any artefact can be traced back to the exact source, prompts, model calls, tool
calls, scores, routing decisions, and user actions that produced it.

The full implementation plan lives in [`plan/`](./plan/) — an overview plus 14 sequential,
TDD-driven phase documents. Start with [`plan/00-overview.md`](./plan/00-overview.md).

## Repository layout

```
groundscribe/
├── backend/          # Python app: domain, workflow, stages, api, cli, provenance
│   ├── src/groundscribe/
│   ├── alembic/      # migrations (baseline in phase 01)
│   └── tests/        # backend unit / contract / provenance tests
├── frontend/         # React + TypeScript (placeholder until phase 11)
├── contracts/        # generated OpenAPI + TS types
├── prompts/          # versioned Jinja2 prompt templates + metadata
├── config/           # versioned operational config (model routing, workflow policy)
├── evaluations/      # golden data, eval datasets, evaluation suite
├── tests/            # cross-cutting / integration tests
├── docker/
├── compose.yaml
└── README.md
```

### Editable files: prompts, routing and workflow policy

These deliberately live outside the code, because they change often and a change
to any of them changes what the system produces:

```
prompts/<template_id>/metadata.yaml   # declared versions + which one is current
prompts/<template_id>/v1.jinja2       # one file per version
config/model-routing.yaml             # per-stage provider/model/params, versioned
config/workflow-policy.yaml           # failure routing, rewrite limits, stagnation
config/scoring-rubric.yaml            # dimension weights + what passes, versioned
config/scoring-rubric-<version>.yaml  # a superseded or candidate rubric, kept
```

Both roots can be pointed elsewhere with `GROUNDSCRIBE_PROMPTS_ROOT` and
`GROUNDSCRIBE_CONFIG_ROOT`. Every model invocation records the prompt version and
routing-policy version it ran under, and every workflow transition records the
workflow-policy version behind it, so a change here never makes an existing
provenance record ambiguous.

A superseded prompt version is kept rather than deleted: a run that produced an
artefact under `v1` must still be able to name and re-render the prompt it
actually used, which is why `metadata.yaml` declares `current_version` instead of
the store inferring it from the highest file on disk. Scoring rubrics work the
same way — `scoring-rubric.yaml` is whichever is current, and an experiment
comparing two of them loads the other by name. A version that is not on disk is
an error rather than a fallback: scoring a candidate under the baseline's rubric
and reporting the two as comparable is the failure worth being loud about.

### Golden data

`evaluations/golden/` holds representative source material and the structured
output a good model returns for it — the fixtures the stage tests run against.
Evaluation *datasets* are a different thing built at runtime: a corpus of
articles a person actually approved, which is what an experiment measures a
candidate configuration over (`writer experiment dataset`). Confidential source
material stays out of one until a project is named as an exception. Golden responses reference source segments by label (`S0`, `S1`,
…) because ids are generated per run; the tests substitute the real ids of a
freshly ingested document.

## Tech stack (fixed by spec)

- **Backend:** Python 3.12+, FastAPI, Pydantic, SQLAlchemy, Alembic, Jinja2, Typer.
- **Storage:** SQLite (local dev) / PostgreSQL (concurrent) — no SQLite-only behaviour.
- **Frontend:** React + TypeScript (Vite), OpenAPI-generated client, SSE for progress.
- **Workflow:** custom Python state machine (not LangGraph/Temporal).
- **Testing:** pytest, pytest-asyncio, Hypothesis, coverage; deterministic fake LLM.

## Local development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
# install dependencies into a local virtualenv
uv sync

# run the full quality gate
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Coverage is enforced in CI. See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the mandatory
test-first workflow and commit discipline.

The suite runs on in-memory SQLite by default, and three switches take it further
— PostgreSQL, and the container stack. They are opt-in because each costs minutes
rather than seconds, and a suite nobody runs is worse than a narrow one; see
[Choosing SQLite or PostgreSQL](#choosing-sqlite-or-postgresql) for the commands.

## Running it

groundscribe is two processes over one database. The API accepts commands and
answers immediately; anything that calls a model is queued, and a **worker** runs
it. Nothing that talks to a provider happens inside an HTTP request.

```bash
# apply migrations to the local SQLite database
uv run alembic upgrade head

# serve the API (interactive docs at /docs)
uv run uvicorn --factory groundscribe.api.asgi:served_app

# drain whatever is queued, recovering anything a previous worker abandoned
uv run writer worker run
```

The CLI is a second front door onto the same application service the API calls —
it contains no workflow logic of its own:

```bash
uv run writer project create --title "…" --author me --audience "…" \
    --platform "…" --depth practitioner --provider ollama
uv run writer source import <project> --title "…" --file notes.md
uv run writer source extract <project>
uv run writer architecture propose <project>
uv run writer architecture approve <project> --by me
uv run writer article draft <article>
uv run writer execution inspect <execution>
uv run writer execution replay <execution> --by me
uv run writer execution fork <execution> --by me --model llama3.1:8b-instruct
uv run writer experiment compare <left> <right>
uv run writer experiment reproducibility
uv run writer experiment dataset "approved work" --created-by me
uv run writer experiment create "cheaper model?" --dataset <id> --created-by me \
    --arm baseline --arm "small=model=llama3.1:8b-instruct"
uv run writer experiment start <experiment>
uv run writer experiment report <experiment>
```

An experiment arm is a *fork*: a label plus the variables it changes, run over a
corpus built from the articles a person approved. The baseline runs too rather
than reusing the numbers already on record — a hosted model may answer
differently the second time, and a candidate compared against a stored figure
would be compared against a different draw as well as a different configuration.
`writer experiment reproducibility` prints what repeating work here does and does
not guarantee; the last clause is the one that says a model is not promised to
repeat itself.

`GROUNDSCRIBE_DATABASE_URL` and `GROUNDSCRIBE_BLOB_ROOT` point the same binaries at
a different installation — note that Alembic reads its URL from `alembic.ini`
rather than the environment, so a custom database has to be named in both places.
On SQLite the engine turns on write-ahead logging and begins transactions
immediately, because the API and the worker are two processes over one file; a
stage holds its transaction for its whole run, so with a slow provider expect a
command issued mid-stage to wait for it. No provider client is registered by default: a
local-first tool that silently reached an external provider would be the opposite
of what it promises, so a stage that needs one fails loudly naming it.

### Signing in

One shared password, in `.env` (see `.env.example`), exchanged at `/auth/login`
for a signed `HttpOnly` cookie that lasts a week. Everything outside `/auth` is
refused without it, including paths that do not exist.

It is a lock on the front door and not much more: no accounts, no TLS, no rate
limiting, and every human action is still attributed to the same author — whoever
holds the password *is* that author as far as the system can tell. The password
travels in clear text over plain HTTP, so the network is the real boundary.

### Confidentiality, retention and export

Every span of source material carries two facts: a classification a person sets —
**publishable**, **internal** or **confidential** — and any extra boundaries they
name on top of it. A classification *implies* exclusions and an explicit flag may
only add, so there is deliberately no way to say "confidential, but do send it".

| | sent to a model | printed in the article | kept in an exported trace |
|---|---|---|---|
| publishable | ✅ | ✅ | ✅ |
| internal | ✅ | ❌ | ✅ |
| confidential | ❌ | ❌ | ❌ |

Two things set them. Importing a document as confidential makes its segments
*internal*, plus an exclusion from exported traces — marking a whole postmortem
sensitive is a request not to publish it, not a request for an article that can
never be written. An inline `[[CONFIDENTIAL]] … [[/CONFIDENTIAL]]` marker is the
strong one, and it means what it already meant everywhere else in the system:
this must not leave the machine.

They are enforced in three places, none of which trusts the others. Context
selection withholds excluded material *before* it competes for the token budget,
recording it as excluded with a reason that names confidentiality rather than the
budget. Final validation fails an article that reprints flagged material
verbatim. The `approve_final` transition refuses it again, so no route to
publication can skip the check.

How much of a trace is kept is the project's declared choice, stored beside its
other constraints so an old run can still say what it was recorded under:

| mode | keeps |
|---|---|
| `full` | every payload, indefinitely |
| `redacted_full` | the same, minus the project's own restricted source material |
| `temporary_raw_retention` | the same, with raw provider payloads expiring after 7 days |
| `no_raw_provider_payloads` | the prompt and the structured output, never the raw response |
| `metadata_and_structured_only` | structured output only |
| `minimal_operational_logging` | no payloads at all |

No mode drops the record that a call happened — a trace that forgot one would not
be a smaller trace, it would be a wrong one — and `full` is not "unredacted":
redaction before persistence is a product principle, not a setting.

```bash
uv run writer article render <version> --format markdown|plain_text|html|clipboard
uv run writer privacy visibility <project>    # who sees this, and what is kept
uv run writer privacy traces <project> --sanitise --report
uv run writer privacy report [<project>]      # what the trace costs on disk
uv run writer privacy forget <project>        # drop the payloads, keep the record
```

A full trace export of a project holding confidential material is **refused**
until the caller says it means to (`--i-know-this-may-contain-confidential-material`,
spelled out because an option called `--force` gets typed by reflex). A sanitised
export needs no such flag, which is what keeps the acknowledgement from becoming
a box people tick.

Setting `GROUNDSCRIBE_ENCRYPT_TRACES` encrypts stored artefact content at rest,
with the key in `GROUNDSCRIBE_TRACE_KEY` or under `GROUNDSCRIBE_KEY_ROOT` —
beside the blob root, never inside it. It is off by default because switching it
on over blobs already on disk would make every one of them unreadable. Addresses
stay on the plaintext, so deduplication and every recorded content hash still
work.

Defects found and consciously left open are in
[KNOWN-ISSUES.md](KNOWN-ISSUES.md), with what reproduces each one.

### Watching it run

Seventeen numbers, over the whole installation or one project:

```bash
uv run writer project metrics            # every project
uv run writer project metrics <project>  # one of them
curl -s localhost:8000/metrics           # the same, for a monitoring check
```

Stage durations, tokens, cost, retries, validation failures, schema-repair and
model-fallback frequency, score change, rewrite count, what became of each review
finding, stagnation, overrides, question response rate, context truncation, tool
failures, human edit distance, final approval rate.

Every one is a **query over the trace**, not a counter kept beside it. Nothing is
incremented at runtime, so a metric cannot drift from the record it summarises,
and any figure can be argued with by opening the rows it read. A rate with nothing
to divide reports `n/a` rather than `0` — "no tool has ever failed" and "no tool
has ever run" are different facts.

The API and worker write **structured logs** — one JSON object per line — carrying
the ids that resolve them: project, article, pipeline run, stage execution, job,
model request, tool invocation, trace event. A log line is a pointer into the
trace rather than a second copy of it, so "something failed overnight" becomes a
query:

```json
{"timestamp": "2026-07-29T11:18:41+00:00", "level": "ERROR", "event": "job.failed",
 "project_id": "…", "pipeline_run_id": "…", "stage_execution_id": "…",
 "job_id": "…", "trace_event_id": "…", "error_type": "LLMTimeoutError"}
```

Ids that are not known are absent rather than null, and secrets are removed
before the record reaches `logging` — not in the formatter, so a deployment that
attaches its own log shipper still receives redacted material.

### The API contract

`contracts/openapi.json` is generated from the app and committed, so a contract
change is reviewed alongside the route that caused it and the web app's generated
client has something stable to build from. Regenerate it after adding or changing
an endpoint — a test fails if it drifts:

```bash
uv run writer contracts export
cd frontend && npm run contract   # and the TypeScript types from it
```

## Installing it

One command, and the whole stack runs in containers:

```bash
scripts/install.sh              # SQLite: three containers, nothing to tune
scripts/install.sh --postgres   # add PostgreSQL, for concurrent use
scripts/install.sh --no-start   # write the configuration, build nothing
```

It checks Docker is reachable, writes a `.env` with a generated password if there
is not one already, builds the images, starts the stack, waits for the API to
answer, and prints where to go. It does not continue past a failure, and it does
not claim success from `compose up` returning zero — if the API never answers it
prints the backend's own log.

Afterwards it is ordinary Compose:

```bash
docker compose up -d                      # the SQLite stack
docker compose --profile postgres up -d   # with the database service
docker compose logs -f worker             # what the background process is doing
docker compose down                       # stop; add -v to delete every artefact
docker compose run --rm backend writer project metrics   # any CLI command
```

| | |
|---|---|
| web app | http://127.0.0.1:3000 |
| API | http://127.0.0.1:8000 (proxied at `/api`, so it need not be exposed) |
| artefacts, key, SQLite file | the `storage` volume |

`GROUNDSCRIBE_API_PORT` and `GROUNDSCRIBE_WEB_PORT` move the published ports if
something already has them.

**The worker is its own container** running the same image as the API with a
different command. They are the same application, so one image; the process must
be separate, or model calls happen inside HTTP requests.

### Choosing SQLite or PostgreSQL

SQLite is the default everywhere — local runs, the container stack, and the test
suite — because a first run should need no server and no credentials beyond the
one password. Move to PostgreSQL when two things want to write at once: a real
provider makes a stage take as long as the model does, and a job holds the SQLite
write lock for its whole run ([KNOWN-ISSUES §1](./KNOWN-ISSUES.md)).

```bash
# containers: adds the database service and points the app at it
scripts/install.sh --postgres

# without containers: anything SQLAlchemy understands
export GROUNDSCRIBE_DATABASE_URL=postgresql+psycopg://user:pass@localhost/groundscribe
uv sync --extra postgres
uv run alembic upgrade head
```

Migrations follow `GROUNDSCRIBE_DATABASE_URL` and fall back to `alembic.ini`, so
the database that gets migrated is always the one the application opens.

The domain avoids SQLite-specific behaviour, and that is tested rather than
asserted:

```bash
docker run -d -p 55432:5432 -e POSTGRES_PASSWORD=groundscribe \
    -e POSTGRES_USER=groundscribe -e POSTGRES_DB=groundscribe postgres:16-alpine

# the designated integration subset — a whole pipeline run, on Postgres
GROUNDSCRIBE_TEST_POSTGRES_URL=postgresql+psycopg://groundscribe:groundscribe@localhost:55432/groundscribe \
    uv run pytest tests/test_postgres_parity.py --no-cov

# or the entire suite against the same server
GROUNDSCRIBE_TEST_DATABASE_URL=postgresql+psycopg://groundscribe:groundscribe@localhost:55432/groundscribe \
    uv run pytest --no-cov

# and the container stack itself, built and started for real
GROUNDSCRIBE_TEST_COMPOSE=1 uv run pytest tests/test_deployment.py --no-cov
```

CI runs both databases on every push.

### Packaging as a desktop app

Deferred, deliberately. The spec's guidance is to wrap groundscribe in
[Tauri](https://tauri.app/) *after* the workflow is validated, and wrapping it
now would freeze an install path while the thing being installed is still
changing. The pieces it would need are already in place: the API binds a port and
holds no session state a wrapper would have to reproduce, `paths.py` takes its
roots from the environment, the OS-keychain path for secrets is written up under
"Confidentiality, retention and export", and the frontend is a static bundle. The
work is a Tauri shell that starts the API and worker as sidecars and points a
webview at the bundle — a packaging job, not a change to the system.

## Everything at once, without containers

```bash
scripts/dev.sh                # migrations, API, worker and web app on 127.0.0.1
scripts/dev.sh --lan          # the same, reachable from the network
```

Three processes, because the system is three: an API that queues work, a worker
that does it, and the web app. `HOST`, `API_PORT`, `WEB_PORT` and `POLL` are the
knobs; Ctrl-C stops all three.

On the first run it writes a random `GROUNDSCRIBE_PASSWORD` into `.env` and
prints it — the API refuses to start without one. Change it there; it is the
only credential this installation has, and the session cookie is signed with a
key derived from it, so changing it signs every browser out.

The processes are also worth knowing individually:

### The web application

A local-first React app over the same API, in `frontend/`. It renders artefacts —
the source model, the brief, the version, the findings, the score sheet, the
trace — and submits commands; it holds no workflow rules of its own, which a
guard test enforces by reading the source for state names, action names and
command URLs.

```bash
cd frontend
npm install
npm run dev        # http://127.0.0.1:5173, proxying /api to the backend on :8000
HOST=0.0.0.0 npm run dev   # the same, on every interface
npm test           # component + contract tests
npm run typecheck  # tsc --strict, the frontend's equivalent of mypy --strict
```

Run the API and a worker alongside it (above); the app reads through `/api` and
follows a running job over SSE.

Two screens' worth of vocabulary comes from the backend rather than the client:
each offered action arrives with the endpoint that performs it, and the trace
filters arrive with the response that uses them. That is deliberate — it is what
lets the frontend render exactly what the backend offers without keeping a
second, drifting copy of the API.
