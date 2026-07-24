"""The three record categories and which tables hold them (phase 03).

groundscribe keeps three kinds of record deliberately apart (plan/03 →
Deliverables):

- **Editorial artefacts** — the source model, architectures, briefs, drafts and
  reviews the product is *for* (phase 02).
- **Execution records** — how those artefacts were produced: runs, stage
  executions, model and tool invocations, decisions, interventions, the trace.
- **Evaluation data** — scores produced by versioned rubrics over an execution's
  output (this phase, extended in phase 12).

They are linked by foreign key, never merged. Merging them is the usual way a
system loses its provenance: once artefact, execution and score share one table
with a JSON payload, every question needs a full scan and a parser, and no
schema constrains what is written.

This module is the single declaration of that partition, and
``test_record_categories`` asserts it covers ``Base.metadata`` exactly — so a
later phase must classify any table it adds rather than leaving the boundary to
drift.
"""

from __future__ import annotations

#: Editorial artefacts: the product's subject matter (phase 02).
EDITORIAL_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "projects",
        "source_documents",
        "source_segments",
        "source_claims",
        "source_claim_segments",
        "source_gaps",
        "user_answers",
        "content_architectures",
        "article_concepts",
        "article_briefs",
        "articles",
        "article_versions",
        "reviews",
        "review_issues",
        "revision_plans",
        "voice_profiles",
        "validation_reports",
        "artifact_snapshots",
    }
)

#: Execution records: how an artefact came to exist (phase 03).
#:
#: ``experiment_runs`` and ``jobs`` are shells here — filled in phases 12 and 09
#: — but they are execution records by nature and are classified now so the
#: partition stays complete.
EXECUTION_TABLES: frozenset[str] = frozenset(
    {
        "pipeline_runs",
        "stage_executions",
        "execution_artifacts",
        "context_selections",
        "context_items",
        "model_invocations",
        "tool_invocations",
        "tool_result_dependencies",
        "decision_records",
        "user_interventions",
        "trace_events",
        "experiment_runs",
        "jobs",
    }
)

#: Evaluation data: scores over an execution's output, under a versioned rubric.
EVALUATION_TABLES: frozenset[str] = frozenset({"evaluation_runs"})
