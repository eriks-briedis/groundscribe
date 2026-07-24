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
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"


def _make_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_upgrade_then_downgrade_round_trips(tmp_path: Path) -> None:
    """A scratch DB upgrades to head and downgrades back to base without error."""
    db_path = tmp_path / "scratch.sqlite"
    db_url = f"sqlite+pysqlite:///{db_path}"
    cfg = _make_config(db_url)

    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine = create_engine(db_url)
    try:
        # downgrading to base clears alembic's version bookkeeping.
        assert "alembic_version" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_baseline_migration_is_empty_and_reversible(tmp_path: Path) -> None:
    """Exactly one baseline revision exists (down_revision None) and creates no schema."""
    cfg = _make_config(f"sqlite+pysqlite:///{tmp_path / 'scratch.sqlite'}")
    script = ScriptDirectory.from_config(cfg)

    revisions = list(script.walk_revisions())
    assert len(revisions) == 1, "phase 01 must ship exactly the empty baseline revision"
    baseline = revisions[0]
    assert baseline.down_revision is None, "baseline must be the root revision"

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'scratch.sqlite'}")
    try:
        # An empty baseline introduces no domain tables — only alembic's own bookkeeping.
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()
