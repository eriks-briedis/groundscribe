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
```

Both roots can be pointed elsewhere with `GROUNDSCRIBE_PROMPTS_ROOT` and
`GROUNDSCRIBE_CONFIG_ROOT`. Every model invocation records the prompt version and
routing-policy version it ran under, and every workflow transition records the
workflow-policy version behind it, so a change here never makes an existing
provenance record ambiguous.

A superseded prompt version is kept rather than deleted: a run that produced an
artefact under `v1` must still be able to name and re-render the prompt it
actually used, which is why `metadata.yaml` declares `current_version` instead of
the store inferring it from the highest file on disk.

### Golden data

`evaluations/golden/` holds representative source material and the structured
output a good model returns for it — the same data phase 12's evaluation suite
scores against. Golden responses reference source segments by label (`S0`, `S1`,
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

## Running it

groundscribe is two processes over one database. The API accepts commands and
answers immediately; anything that calls a model is queued, and a **worker** runs
it. Nothing that talks to a provider happens inside an HTTP request.

```bash
# apply migrations to the local SQLite database
uv run alembic upgrade head

# serve the API (interactive docs at /docs)
uv run uvicorn groundscribe.api.asgi:app

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
uv run writer experiment compare <left> <right>
```

`GROUNDSCRIBE_DATABASE_URL` and `GROUNDSCRIBE_BLOB_ROOT` point the same binaries at
a different installation. No provider client is registered by default: a
local-first tool that silently reached an external provider would be the opposite
of what it promises, so a stage that needs one fails loudly naming it.

### The API contract

`contracts/openapi.json` is generated from the app and committed, so a contract
change is reviewed alongside the route that caused it and the web app's generated
client has something stable to build from. Regenerate it after adding or changing
an endpoint — a test fails if it drifts:

```bash
uv run writer contracts export
cd frontend && npm run contract   # and the TypeScript types from it
```

### The web application

A local-first React app over the same API, in `frontend/`. It renders artefacts —
the source model, the brief, the version, the findings, the score sheet, the
trace — and submits commands; it holds no workflow rules of its own, which a
guard test enforces by reading the source for state names, action names and
command URLs.

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173, proxying /api to the backend on :8000
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
