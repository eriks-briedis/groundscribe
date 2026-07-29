"""The confidentiality columns, as the migrations build them (phase 13).

Spec (plan/13 → *Confidentiality flags* on source claims/segments). Written
against a database built only by ``alembic upgrade``, never by the ORM's
``create_all``: the enforcement points added in this phase read these columns on
every request, and a schema that exists in the mapped metadata but not in the
migrations would pass every other test in the suite and fail on the one machine
that matters.

Existing rows are the part worth pinning. A source segment ingested before this
phase has no flag, and the only safe reading of "no flag" is *publishable* — the
material has already been sent to a provider and printed in an article. Any other
default would retroactively make past runs violations.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"

FLAG_COLUMNS = {"confidentiality", "excluded"}

#: The revision this phase's columns were added on top of.
BEFORE = "0018_experiments"


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def migrated_url(tmp_path: Path) -> str:
    """A database at head, built only by running the migrations."""
    url = f"sqlite+pysqlite:///{tmp_path / 'confidentiality.sqlite'}"
    command.upgrade(_config(url), "head")
    return url


@pytest.mark.parametrize("table", ["source_segments", "source_claims"])
def test_both_tables_carry_the_flags(migrated_url: str, table: str) -> None:
    """Segments and claims are flagged independently, so both tables carry them."""
    engine = create_engine(migrated_url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns(table)}
        assert columns >= FLAG_COLUMNS
    finally:
        engine.dispose()


def test_material_ingested_before_this_phase_reads_as_publishable(tmp_path: Path) -> None:
    """A row written under the old schema is publishable, not null.

    Upgrading over live data is the case: the column arrives with a server
    default so the rows already on disk answer the question, rather than
    answering ``NULL`` and forcing every enforcement point to grow a branch for
    material it cannot classify.
    """
    url = f"sqlite+pysqlite:///{tmp_path / 'upgrade-in-place.sqlite'}"
    cfg = _config(url)
    command.upgrade(cfg, BEFORE)

    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO source_documents (id, schema_version, project_id, title, "
                    "media_type, source_format, content_hash, confidential, branch_status, "
                    "selection_status) VALUES ('doc-old', 1, 'proj-old', 'Postmortem', "
                    "'text/markdown', 'markdown', '', 0, 'active', 'pending')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO source_segments (id, schema_version, document_id, ordinal, "
                    "text, kind, content_hash, char_start, char_end) VALUES "
                    "('seg-old', 1, 'doc-old', 0, 'latency fell to 120ms', 'paragraph', "
                    "'', 0, 21)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT confidentiality, excluded FROM source_segments WHERE id = 'seg-old'")
            ).one()
        assert row.confidentiality == "publishable"
        assert row.excluded == "[]"
    finally:
        engine.dispose()


def test_the_columns_downgrade_away(migrated_url: str) -> None:
    """Reversible, like every other migration in the tree."""
    command.downgrade(_config(migrated_url), BEFORE)
    engine = create_engine(migrated_url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("source_segments")}
        assert not (FLAG_COLUMNS & columns)
    finally:
        engine.dispose()
