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
        "project_constraints",
        "source_documents",
        "source_segments",
        "source_claims",
        "source_claim_segments",
        "source_gaps",
        "user_answers",
        "user_answer_gaps",
        "content_architectures",
        "article_concepts",
        "article_briefs",
        "articles",
        "article_versions",
        "reviews",
        "review_issues",
        "revision_plans",
        "voice_profiles",
        # Phase 10. A profile version is an editorial artefact — it is part of
        # what the author is writing, not a record of how something ran — and a
        # hand edit is the author's own contribution to one.
        "voice_profile_versions",
        "manual_edits",
        "voice_suggestions",
        "validation_reports",
        "artifact_snapshots",
    }
)

#: Execution records: how an artefact came to exist (phase 03).
#:
#: ``experiment_runs`` is a shell here — filled in phase 12 — but it is an
#: execution record by nature and is classified now so the partition stays
#: complete.
#:
#: ``workflow_positions`` (phase 09) is the odd one and is classified here
#: deliberately. It is not an artefact and not a score; it is *where a run has
#: got to*, which is a fact about the execution. Unlike its neighbours it is
#: mutable — a position moves — but the alternative, a fourth category holding
#: one table, would draw a line through the partition to describe a lifetime
#: rather than a kind of record.
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
        "workflow_positions",
    }
)

#: Evaluation data: scores over an execution's output, under a versioned rubric.
EVALUATION_TABLES: frozenset[str] = frozenset({"evaluation_runs"})
