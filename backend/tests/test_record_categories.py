"""Record-separation tests (phase 03).

Spec (plan/03 → Deliverables and Exit criteria): editorial artefacts, execution
records and evaluation data are *linked but not stored as one unstructured event
stream*. The failure mode this guards against is the common one — everything
becomes an ``events`` table with a JSON blob, and afterwards no question can be
answered without parsing every payload.

The strongest assertion here is coverage: every mapped table must be classified
into exactly one category. A future phase that adds a table has to say which
kind of record it holds, and a phase that tries to merge the categories fails
this test rather than passing review unnoticed.
"""

from __future__ import annotations

from groundscribe.db import Base
from groundscribe.records import EDITORIAL_TABLES, EVALUATION_TABLES, EXECUTION_TABLES

ALL_CATEGORIES = (EDITORIAL_TABLES, EXECUTION_TABLES, EVALUATION_TABLES)


def test_every_mapped_table_is_classified_into_exactly_one_category() -> None:
    """No table may be unclassified, and none may belong to two categories."""
    mapped = set(Base.metadata.tables)
    classified = EDITORIAL_TABLES | EXECUTION_TABLES | EVALUATION_TABLES

    assert classified == mapped, {
        "unclassified": mapped - classified,
        "unknown": classified - mapped,
    }
    total = sum(len(category) for category in ALL_CATEGORIES)
    assert total == len(classified), "a table is claimed by more than one category"


def test_each_category_is_populated() -> None:
    """An empty category would make the separation vacuously true."""
    for category in ALL_CATEGORIES:
        assert category


def test_evaluation_scores_are_not_folded_into_the_execution_row() -> None:
    """Scores live in their own linked table, not as columns on the execution.

    Folding them in is what makes evaluation data un-versionable: a second
    rubric applied to the same execution would have nowhere to go.
    """
    execution_columns = set(Base.metadata.tables["stage_executions"].columns.keys())
    assert not execution_columns & {"scores", "rubric_version", "evaluator_id", "passed"}

    evaluation = Base.metadata.tables["evaluation_runs"]
    assert {fk.column.table.name for fk in evaluation.foreign_keys} == {"stage_executions"}


def test_execution_detail_is_not_copied_onto_editorial_artefacts() -> None:
    """An editorial row references its execution; it does not embed one.

    plan/00: every artefact references a creating execution. A *reference* — not
    a copy of the prompt and response, which would put two divergent versions of
    the same fact in the database.
    """
    forbidden = {"prompt", "rendered_prompt", "raw_response", "messages", "provider"}
    for name in EDITORIAL_TABLES:
        columns = set(Base.metadata.tables[name].columns.keys())
        assert not columns & forbidden, f"{name} embeds execution detail"


def test_editorial_entities_carry_the_link_to_their_creating_execution() -> None:
    """Every editorial entity table (not the pure association tables) has the hook."""
    for name in EDITORIAL_TABLES:
        table = Base.metadata.tables[name]
        if "id" not in table.columns:  # association tables have no identity of their own
            continue
        assert "created_by_execution_id" in table.columns, name


def test_execution_records_hang_off_the_stage_execution_not_off_the_trace() -> None:
    """Each execution detail table is reachable by FK, without reading a trace payload.

    This is what makes the trace a *timeline* rather than the system of record:
    losing or truncating trace events must not lose any invocation, decision or
    intervention.
    """
    anchored = {
        "context_selections",
        "model_invocations",
        "tool_invocations",
        "decision_records",
        "user_interventions",
        "execution_artifacts",
    }
    for name in anchored:
        table = Base.metadata.tables[name]
        assert "stage_execution_id" in table.columns, name
        targets = {fk.column.table.name for fk in table.columns["stage_execution_id"].foreign_keys}
        assert targets == {"stage_executions"}, name


def test_trace_events_are_classified_as_execution_records() -> None:
    """The trace is one execution record among many, not a category of its own."""
    assert "trace_events" in EXECUTION_TABLES
    assert len(EXECUTION_TABLES) > 1
