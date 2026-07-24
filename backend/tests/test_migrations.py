"""Alembic baseline migration tests (phase 01).

Spec (plan/01 Test-first specification → Alembic migration tests):
- ``upgrade head`` then ``downgrade base`` succeeds on a scratch SQLite DB;
- the baseline migration is empty/no-op and reversible.

Later phases add real schema; this locks in that migrations round-trip and the
baseline starts from a clean slate.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"


def _make_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def test_upgrade_then_downgrade_round_trips(tmp_path: Path) -> None:
    """A scratch DB upgrades to head and downgrades back to base without error."""
    db_path = tmp_path / "scratch.sqlite"
    db_url = f"sqlite+pysqlite:///{db_path}"
    cfg = _make_config(db_url)

    head = ScriptDirectory.from_config(cfg).get_current_head()
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
        assert _current_revision(engine) == head
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine = create_engine(db_url)
    try:
        # Downgrading to base leaves the empty bookkeeping table but no current revision.
        assert _current_revision(engine) is None
    finally:
        engine.dispose()


def test_baseline_migration_is_empty_and_reversible(tmp_path: Path) -> None:
    """Exactly one root revision exists (down_revision None) and it creates no schema."""
    db_url = f"sqlite+pysqlite:///{tmp_path / 'scratch.sqlite'}"
    cfg = _make_config(db_url)
    script = ScriptDirectory.from_config(cfg)

    roots = [rev for rev in script.walk_revisions() if rev.down_revision is None]
    assert len(roots) == 1, "there must be exactly one root revision"
    baseline = roots[0]
    assert baseline.revision == "0001_baseline"

    # Upgrading to the baseline specifically introduces no domain tables — only
    # alembic's own bookkeeping. Later revisions add the schema (tested elsewhere).
    command.upgrade(cfg, baseline.revision)
    engine = create_engine(db_url)
    try:
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()
