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
├── evaluations/      # golden data, eval datasets, evaluation suite
├── tests/            # cross-cutting / integration tests
├── docker/
├── compose.yaml
└── README.md
```

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
