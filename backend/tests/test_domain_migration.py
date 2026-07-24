"""Editorial-schema migration tests (phase 02).

Spec (plan/02 → Test-first specification): the new domain tables ``upgrade`` /
``downgrade`` cleanly. This locks in that the phase-02 revision creates every
editorial table on the way up and removes them all on the way down, so the
SQLite-dev / Postgres-prod migration path stays trivial (plan/00 → Tech stack).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"

EXPECTED_TABLES = {
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


def _make_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_editorial_tables_upgrade_and_downgrade(tmp_path: Path) -> None:
    """Upgrading to head creates every editorial table; downgrading drops them all."""
    db_url = f"sqlite+pysqlite:///{tmp_path / 'scratch.sqlite'}"
    cfg = _make_config(db_url)

    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert tables >= EXPECTED_TABLES, EXPECTED_TABLES - tables
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine = create_engine(db_url)
    try:
        remaining = set(inspect(engine).get_table_names())
        # Only alembic's own bookkeeping survives a full downgrade.
        assert remaining == {"alembic_version"}
    finally:
        engine.dispose()
